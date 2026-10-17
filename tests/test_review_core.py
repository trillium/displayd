"""Review decision page: parsing, flow, drawn variables, touch resolve."""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import review_decision as rd
import review_view as rv
from review_flow import ReviewFlow
from surface_model import SurfaceError
from touch_resolve import action_request

ROOT = os.path.join(os.path.dirname(__file__), os.pardir)
HOLD = ("PR is merged; its config exposes secrets. Options: (1) open a follow-up PR "
        "deleting the block (recommended) or narrowing it; (2) leave as is. Upstream "
        "write needs captain's word. Report: data/x.md\n\n"
        "Firstmate held task x for the captain.\n"
        'Answer with: review human respond review-abc "your decision"\n'
        "The answer is relayed to Firstmate (--resolve-key k).\n\ncorrelationId: \n")


def bead(**kw):
    out = {"id": "review-abc", "title": "Pick one", "status": "open",
           "labels": ["human"], "description": HOLD, "metadata": {}}
    out.update(kw)
    return out


class TestParse(unittest.TestCase):
    def test_enumerated_description_becomes_options(self):
        d = rd.parse_bead([bead()])  # `review show --json` is a one-element list
        self.assertEqual([o["id"] for o in d["options"]], ["1", "2"])
        self.assertEqual(d["options"][1]["text"], "leave as is")
        self.assertEqual(d["options"][1]["value"], "Option 2: leave as is")
        self.assertTrue(d["actionable"] and not d["structured"])

    def test_relay_boilerplate_is_not_context(self):
        ctx = rd.parse_bead(bead())["context"].lower()
        for junk in rd.BOILERPLATE:
            self.assertNotIn(junk, ctx)
        self.assertIn("firstmate held task", ctx)

    def test_structured_options_win_and_carry_their_action(self):
        meta = {"decision": {"options": [
            "Ship it", {"id": "later", "label": "Later", "value": "defer"},
            {"id": "no", "label": "No", "action": "dismiss"}]}}
        d = rd.parse_bead(bead(metadata=meta))
        self.assertTrue(d["structured"])
        self.assertEqual([(o["id"], o["action"], o["value"]) for o in d["options"]],
                         [("1", "respond", "Ship it"), ("later", "respond", "defer"),
                          ("no", "dismiss", "No")])

    def test_bad_structured_options_fall_back_not_crash(self):
        for meta in ({"decision": {"options": "x"}}, {"decision": {"options": [5]}},
                     {"decision": {"options": ["a", {"id": "1", "label": "b"}]}}):
            d = rd.parse_bead(bead(metadata=meta))
            self.assertFalse(d["structured"], meta)

    def test_not_actionable_says_why(self):
        for fields, why in (({"status": "closed"}, "closed"),
                            ({"labels": ["human-only"]}, "human"),
                            ({"description": "no choices here"}, "no options")):
            d = rd.parse_bead(bead(**fields))
            self.assertFalse(d["actionable"])
            self.assertIn(why, d["reason"])

    def test_caps_options_and_fits_labels(self):
        text = " ".join("(%d) %s;" % (i, "w" * 200) for i in range(1, 8))
        d = rd.parse_bead(bead(description=text))
        self.assertEqual(len(d["options"]), rd.MAX_OPTIONS)
        self.assertTrue(all(len(o["label"]) <= rd.LABEL_CHARS for o in d["options"]))
        self.assertEqual(len(d["options"][0]["text"]), 200)  # full text kept

    def test_out_of_order_markers_are_not_options(self):
        self.assertEqual(rd.parse_bead(bead(description="(2) b (1) a"))["options"], [])
        self.assertEqual(rd.parse_bead(bead(description="only (1) here"))["options"], [])

    def test_garbage_is_a_value_error(self):
        for raw in (None, [], [1, 2], {"id": "../etc"}, {"id": "review-a b"}, "x"):
            with self.assertRaises(ValueError, msg=raw):
                rd.parse_bead(raw)


class TestFlow(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.flow = ReviewFlow(self.sent.append, clock=lambda: 7.0)
        self.decision = rd.parse_bead(bead())

    def refused(self, fn, *args):
        with self.assertRaises(SurfaceError) as ctx:
            fn(*args)
        self.assertEqual(ctx.exception.status, 409)

    def test_happy_path_sends_once_with_ids_only(self):
        self.flow.load(self.decision)
        self.assertEqual(self.flow.select(2)["state"], "confirming")
        self.assertEqual(self.sent, [])  # choosing sends nothing
        snap = self.flow.confirm()
        self.assertEqual(snap["state"], "sending")
        self.assertEqual(len(self.sent), 1)
        event = self.sent[0]
        self.assertEqual((event["bead"], event["option"], event["action"]),
                         ("review-abc", "2", "respond"))
        self.assertEqual(sorted(event), ["action", "bead", "nonce", "option"])
        self.assertEqual(self.flow.result(event["nonce"], True, "closed")["state"], "done")

    def test_doubled_or_stale_taps_do_nothing(self):
        self.refused(self.flow.confirm)       # nothing loaded
        self.flow.load(self.decision)
        self.refused(self.flow.confirm)       # nothing chosen
        self.refused(self.flow.select, 3)     # no such option
        self.refused(self.flow.select, True)
        self.flow.select(1)
        self.refused(self.flow.select, 2)     # must confirm or go back first
        self.flow.confirm()
        self.refused(self.flow.confirm)       # already sending
        self.refused(self.flow.back)
        self.refused(self.flow.close)         # the result is still owed
        self.assertEqual(len(self.sent), 1)

    def test_wrong_nonce_result_is_refused(self):
        self.flow.load(self.decision)
        self.flow.select(1)
        self.flow.confirm()
        self.refused(self.flow.result, "other", True)
        self.assertEqual(self.flow.snapshot()["state"], "sending")

    def test_failure_can_retry_or_go_back(self):
        self.flow.load(self.decision)
        self.flow.select(1)
        self.flow.confirm()
        self.flow.result(self.sent[0]["nonce"], False, "bead closed")
        self.assertEqual(self.flow.snapshot()["message"], "bead closed")
        self.assertEqual(self.flow.confirm()["state"], "sending")
        self.assertNotEqual(self.sent[0]["nonce"], self.sent[1]["nonce"])
        self.flow.result(self.sent[1]["nonce"], False, "again")
        self.assertEqual(self.flow.back()["state"], "showing")

    def test_a_failing_publish_is_reported_not_swallowed(self):
        def boom(_event):
            raise RuntimeError("no stream")
        flow = ReviewFlow(boom)
        flow.load(self.decision)
        flow.select(1)
        snap = flow.confirm()
        self.assertEqual(snap["state"], "failed")
        self.assertIn("no stream", snap["message"])

    def test_read_only_bead_cannot_be_chosen(self):
        self.flow.load(rd.parse_bead(bead(status="closed")))
        self.refused(self.flow.select, 1)

    def test_loading_replaces_even_mid_confirm(self):
        self.flow.load(self.decision)
        self.flow.select(1)
        snap = self.flow.load(rd.parse_bead(bead(id="review-zzz")))
        self.assertEqual((snap["state"], snap["chosen"]), ("showing", None))


class TestView(unittest.TestCase):
    def snap(self, **kw):
        base = {"state": "showing", "decision": rd.parse_bead(bead()), "chosen": None,
                "message": None, "nonce": None, "at": 1.0}
        base.update(kw)
        return base

    def template(self):
        with open(os.path.join(ROOT, "html-templates", rv.TEMPLATE)) as fh:
            return fh.read()

    def test_every_template_variable_is_supplied_and_nothing_else(self):
        names = set(re.findall(r"\{\{(\w+)\}\}", self.template()))
        for state in ("idle", "showing", "confirming", "sending", "done", "failed"):
            snap = self.snap(state=state, chosen=rd.parse_bead(bead())["options"][0],
                             message="m")
            if state == "idle":
                snap["decision"] = None
            self.assertEqual(set(rv.build_vars(snap)), names, state)

    def test_showing_lists_numbered_options_blank_slots_quiet(self):
        out = rv.build_vars(self.snap())
        self.assertTrue(out["o1"].startswith("1 ") and "follow-up" in out["o1"])
        self.assertIn("leave as is", out["o2"])
        self.assertEqual((out["o3"], out["o4"], out["o5"]), ("", "", ""))
        self.assertEqual((out["foot_left"], out["foot_right"]), ("CLOSE", ""))

    def test_confirming_names_the_full_answer_and_offers_confirm(self):
        chosen = rd.parse_bead(bead())["options"][1]
        out = rv.build_vars(self.snap(state="confirming", chosen=chosen))
        self.assertIn("leave as is", out["context"])
        self.assertEqual((out["foot_left"], out["foot_right"]), ("BACK", "CONFIRM"))
        self.assertEqual(out["o1"], "")

    def test_read_only_says_why_and_lists_no_buttons(self):
        d = rd.parse_bead(bead(labels=["human-only"]))
        out = rv.build_vars(self.snap(decision=d))
        self.assertIn("Read only", out["context"])
        self.assertEqual(out["o1"], "")

    def test_failed_offers_retry(self):
        out = rv.build_vars(self.snap(state="failed", message="bead closed"))
        self.assertIn("bead closed", out["context"])
        self.assertEqual((out["foot_left"], out["foot_right"]), ("BACK", "RETRY"))

    def test_text_is_bounded_whatever_the_bead_says(self):
        d = rd.parse_bead(bead(title="t" * 900, description="d" * 9000))
        out = rv.build_vars(self.snap(decision=d))
        self.assertLessEqual(len(out["title"]), rv.TITLE_CHARS)
        self.assertLessEqual(len(out["context"]), rv.CONTEXT_CHARS)

    def test_regions_match_the_template_metrics(self):
        css = self.template()
        px = lambda rule, prop: int(re.search(  # noqa: E731
            r"\.%s\s*\{[^}]*?\b%s:\s*(\d+)px" % (rule, prop), css).group(1))
        head = px("head", "height") + 6                 # + its 6px border
        ctx = px("context", "height") + 16 + 14         # + vertical padding
        meta = px("meta", "height") + 6                 # + top padding
        pitch = px("opt", "height") + px("opt", "margin-bottom") + 2
        self.assertEqual(head + ctx + meta, rv.ROWS_Y)
        self.assertEqual(pitch, rv.ROW_PITCH)
        self.assertEqual(rv.ROWS_Y + 5 * pitch + px("foot", "margin-top"), rv.FOOT_Y)
        self.assertEqual(px("foot", "height"), rv.DESIGN_H - rv.FOOT_Y)
        self.assertEqual(px("half", "width") * 2, rv.DESIGN_W)

    def test_regions_scale_stay_on_panel_and_never_overlap(self):
        for w, h in ((1920, 1080), (1280, 720), (800, 480)):
            regions = rv.review_regions(w, h)
            self.assertEqual(len({r["id"] for r in regions}), len(regions))
            boxes = [r["rect"] for r in regions]
            for x, y, rw, rh in boxes:
                self.assertTrue(0 <= x and 0 <= y and x + rw <= w and y + rh <= h, (w, h))
            for i, a in enumerate(boxes):
                for b in boxes[i + 1:]:
                    apart = (a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0]
                             or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1])
                    self.assertTrue(apart, (a, b, w, h))

    def test_shipped_example_is_generated_from_review_regions(self):
        with open(os.path.join(ROOT, "touch-review.json.example")) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["view_regions"]["review"], rv.review_regions(1920, 1080))


class TestSummary(unittest.TestCase):
    LONG = bead(description="x " * 400, metadata={})

    def setUp(self):
        import review_summary
        self.rs = review_summary
        self.addCleanup(self.rs.set_provider, None)
        self.env = {self.rs.ENV: "1"}

    def parsed(self, **kw):
        return rd.parse_bead(dict(self.LONG, **kw))

    def test_off_by_default_and_without_a_provider(self):
        d = self.parsed()
        self.rs.set_provider(lambda text: "short")
        self.assertEqual(self.rs.apply(d, {}), d)           # env off
        self.rs.set_provider(None)
        self.assertEqual(self.rs.apply(d, self.env), d)     # nothing registered

    def test_summarizes_long_unstructured_context_labelled_and_bounded(self):
        seen = []
        self.rs.set_provider(lambda text: seen.append(text) or "s " * 900)
        d = self.rs.apply(self.parsed(), self.env)
        self.assertEqual(seen, [self.parsed()["context"]])  # context only
        self.assertLessEqual(len(d["summary"]), self.rs.MAX_CHARS)
        self.assertEqual(d["options"], self.parsed()["options"])
        snap = {"state": "showing", "decision": d, "chosen": None, "message": None}
        self.assertIn(self.rs.LABEL, rv.build_vars(snap)["context"])

    def test_structured_short_and_failing_cases_stay_plain(self):
        self.rs.set_provider(lambda text: "summary")
        structured = self.parsed(metadata={"decision": {"options": ["a", "b"]}})
        self.assertNotIn("summary", self.rs.apply(structured, self.env))
        self.assertNotIn("summary", self.rs.apply(rd.parse_bead(bead()), self.env))

        def broken(_text):
            raise RuntimeError("model down")
        self.rs.set_provider(broken)
        self.assertNotIn("summary", self.rs.apply(self.parsed(), self.env))


class TestTouchActions(unittest.TestCase):
    def test_every_region_resolves_to_a_fixed_review_request(self):
        for region in rv.review_regions():
            method, path, body = action_request(region["action"], panel=(1920, 1080))
            self.assertEqual(method, "POST")
            self.assertTrue(path.startswith("/review/"), path)
            self.assertLessEqual(set(body), {"slot"})

    def test_bad_slots_are_denied(self):
        for slot in (0, 6, True, "1", None, 1.0):
            with self.assertRaises(ValueError, msg=slot):
                action_request({"name": "review_select", "slot": slot})


if __name__ == "__main__":
    unittest.main()
