"""Review decision page over the real daemon API, plus the Mac bridge."""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "tools"))

import review_bridge as bridge  # noqa: E402
from test_review_core import bead  # noqa: E402
from test_surfaces_http import SurfaceHttpTestCase  # noqa: E402


class FakeStore:
    """Stands in for the ``review`` CLI: a bead dict plus a log of mutations."""

    def __init__(self, **kw):
        import json
        self.bead, self.json, self.calls = bead(**kw), json, []

    def __call__(self, argv, capture_output=True, text=True, timeout=0):
        self.calls.append(argv[1:])
        if argv[1] == "show":
            return types.SimpleNamespace(returncode=0, stderr="",
                                         stdout=self.json.dumps([self.bead]))
        self.bead["status"] = "closed"
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")


class ReviewHttp(SurfaceHttpTestCase):
    def post(self, path, body=None):
        return self.call("POST", path, {} if body is None else body)

    def load(self, **kw):
        code, out = self.post("/review/load", {"bead": bead(**kw)})
        self.assertEqual(code, 200, out)
        return out


class TestWire(ReviewHttp):
    def test_taps_are_refused_until_a_bead_is_showing(self):
        self.assertEqual(self.call("GET", "/review")[1]["state"], "idle")
        for path, body in (("select", {"slot": 1}), ("confirm", {}), ("back", {})):
            code, out = self.post("/review/" + path, body)
            self.assertEqual(code, 409, (path, out))
            self.assertIn("not showing", out["error"])

    def test_load_shows_the_view_with_prefilled_options(self):
        out = self.load()
        self.assertEqual((out["state"], out["showing"]), ("showing", True))
        self.assertEqual(self.daemon.current, "review")
        self.assertIn("follow-up", out["vars"]["o1"])
        self.assertIn("leave as is", out["vars"]["o2"])
        self.assertEqual(self.daemon.current_params["vars"]["o1"], out["vars"]["o1"])

    def test_the_review_view_stays_out_of_the_picker_tile_set(self):
        import renderers.picker as picker
        self.assertIn("review", self.daemon.renderers)
        self.assertNotIn("review", picker.live_views(self.daemon.renderers))

    def test_bad_beads_are_400_and_change_nothing(self):
        for body in ({}, {"bead": 5}, {"bead": {"id": "nope"}}):
            self.assertEqual(self.post("/review/load", body)[0], 400, body)
        self.assertEqual(self.daemon.current, None)

    def test_select_confirm_publishes_one_decision_and_result_closes_it(self):
        self.load()
        code, out = self.post("/review/select", {"slot": 2})
        self.assertEqual((code, out["state"]), (200, "confirming"))
        self.assertEqual(self.call("GET", "/event-streams/review-decisions/events")[1]["events"], [])
        code, out = self.post("/review/confirm")
        self.assertEqual((code, out["state"]), (200, "sending"))
        events = self.call("GET", "/event-streams/review-decisions/events")[1]["events"]
        self.assertEqual(len(events), 1)
        data = events[0]["data"]
        self.assertEqual((data["bead"], data["option"], data["action"]),
                         ("review-abc", "2", "respond"))
        self.assertEqual(self.post("/review/confirm")[0], 409)  # no double send
        self.assertEqual(self.post("/review/result", {"nonce": "x", "ok": True})[0], 409)
        code, out = self.post("/review/result", {"nonce": data["nonce"], "ok": True,
                                                 "message": "respond review-abc"})
        self.assertEqual((code, out["state"]), (200, "done"))
        self.assertEqual(out["vars"]["eyebrow"], "SENT")

    def test_back_steps_then_returns_to_the_previous_view(self):
        self.daemon.show("clock", {})
        self.load()
        self.post("/review/select", {"slot": 1})
        self.assertEqual(self.post("/review/back")[1]["state"], "showing")
        self.assertEqual(self.daemon.current, "review")
        self.assertEqual(self.post("/review/back")[1]["state"], "idle")
        self.assertEqual(self.daemon.current, "clock")

    def test_a_tap_cannot_act_once_another_view_is_showing(self):
        self.load()
        self.daemon.show("clock", {})
        self.assertEqual(self.post("/review/select", {"slot": 1})[0], 409)
        self.assertEqual(self.daemon.review_flow.snapshot()["state"], "showing")

    def test_read_only_bead_has_no_selectable_option(self):
        self.load(labels=["human-only"])
        self.assertEqual(self.post("/review/select", {"slot": 1})[0], 409)

    def test_token_gate_covers_review_routes(self):
        import displayd
        from unittest import mock
        with mock.patch.object(displayd, "API_TOKEN", b"s3cret"):
            self.assertEqual(self.call("GET", "/review")[0], 401)
            self.assertEqual(self.call("GET", "/review", token="s3cret")[0], 200)
            self.assertEqual(self.call("POST", "/review/load", {"bead": bead()})[0], 401)


class TestBridge(ReviewHttp):
    def decide(self, slot):
        self.load()
        self.post("/review/select", {"slot": slot})
        self.post("/review/confirm")

    def test_load_command_puts_a_bead_on_the_panel(self):
        bridge.load(self.base, "review-abc", "review", FakeStore())
        self.assertEqual(self.daemon.review_flow.snapshot()["state"], "showing")

    def test_serve_runs_the_awaited_decision_and_reports_back(self):
        store = FakeStore()
        self.decide(2)
        bridge.serve(self.base, "review", store, once=True)
        self.assertEqual(store.calls[-1],
                         ["human", "respond", "review-abc", "Option 2: leave as is"])
        snap = self.call("GET", "/review")[1]
        self.assertEqual((snap["state"], snap["message"]), ("done", "respond review-abc"))

    def test_serve_never_replays_a_decision_the_panel_is_not_awaiting(self):
        store = FakeStore()
        self.decide(1)
        self.daemon.review_flow.result(self.daemon.review_flow.snapshot()["nonce"], True)
        bridge.serve(self.base, "review", store, once=True)
        self.assertEqual([c for c in store.calls if c[0] != "show"], [])

    def test_a_bead_closed_meanwhile_is_refused_not_sent(self):
        store = FakeStore()
        self.decide(1)
        store.bead["status"] = "closed"
        bridge.serve(self.base, "review", store, once=True)
        self.assertEqual([c for c in store.calls if c[0] == "human"], [])
        snap = self.call("GET", "/review")[1]
        self.assertEqual(snap["state"], "failed")
        self.assertIn("closed", snap["message"])


class TestExecute(unittest.TestCase):
    event = {"nonce": "n", "bead": "review-abc", "option": "1", "action": "respond"}

    def test_respond_and_dismiss_are_argv_lists(self):
        store = FakeStore(metadata={"decision": {"options": [
            {"id": "1", "label": "-rf yes"}, {"id": "d", "label": "No", "action": "dismiss"}]}})
        self.assertEqual(bridge.execute(self.event, "review", store)[0], True)
        self.assertEqual(store.calls[-1], ["human", "respond", "review-abc", "Answer: -rf yes"])
        store.bead["status"] = "open"
        ok, _ = bridge.execute(dict(self.event, option="d", action="dismiss"), "review", store)
        self.assertTrue(ok)
        self.assertEqual(store.calls[-1], ["human", "dismiss", "review-abc"])

    def test_every_forged_event_is_refused_before_any_mutation(self):
        for forged in ({"bead": "review-abc; rm -rf ~", "option": "1", "action": "respond"},
                       {"bead": "review-abc", "option": "9", "action": "respond"},
                       {"bead": "review-abc", "option": "1", "action": "dismiss"},
                       {"bead": "review-abc"}, {}, {"bead": 5, "option": 1, "action": "x"}):
            store = FakeStore()
            ok, message = bridge.execute(forged, "review", store)
            self.assertFalse(ok, forged)
            self.assertTrue(message.startswith("refused"), message)
            self.assertEqual([c for c in store.calls if c[0] == "human"], [], forged)

    def test_a_store_failure_is_reported_not_raised(self):
        def broken(argv, **_kw):
            raise OSError("no such file")
        ok, message = bridge.execute(self.event, "review", broken)
        self.assertFalse(ok)
        self.assertIn("refused", message)


if __name__ == "__main__":
    unittest.main()
