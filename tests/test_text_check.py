"""The text-fit gate: lines that escape their declared box are caught.

Rules and the Pillow hook (``ui.textbox``), the clip-and-declare path
(``ui.line``), the offscreen probe (``text_audit``), the daemon report
(``GET /text/check``) and the deliberate-failure demonstration.
"""

import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("", "renderers"):
    sys.path.insert(0, os.path.join(ROOT, sub))

from PIL import Image, ImageDraw  # noqa: E402

import renderer_registry  # noqa: E402
import text_audit  # noqa: E402
import text_stress  # noqa: E402
from screen import Screen  # noqa: E402
from ui import line as ui_line  # noqa: E402
from ui import text as ui_text  # noqa: E402
from ui import textbox  # noqa: E402

HAVE_FONT = Screen.font_path("DejaVuSans") is not None


def _entry(ink, box=None, frame=(1920, 1080), text="x", source=None,
           cut=True):
    return {"text": text, "source": source or text, "ink": ink, "box": box,
            "frame": frame, "cut": cut}


class TestRules(unittest.TestCase):
    def rules(self, entry):
        return [v["rule"] for v in textbox.violations(entry)]

    def test_inside_is_clean(self):
        self.assertEqual(self.rules(_entry((10, 10, 90, 40), (0, 0, 100, 50))), [])

    def test_wider_and_taller(self):
        self.assertIn("wider", self.rules(_entry((0, 0, 300, 20), (0, 0, 100, 50))))
        self.assertIn("taller", self.rules(_entry((0, 0, 50, 90), (0, 0, 100, 50))))

    def test_outside_box_names_the_side(self):
        got = textbox.violations(_entry((60, 0, 140, 20), (0, 0, 100, 50)))
        self.assertEqual([(v["rule"], v["side"]) for v in got],
                         [("outside-box", "right")])

    def test_outside_frame(self):
        got = textbox.violations(_entry((-5, 0, 80, 20), None))
        self.assertEqual(got[0]["rule"], "outside-frame")

    def test_open_edges_are_unbounded(self):
        self.assertEqual(self.rules(_entry((0, 0, 90, 900), (0, None, 100, None))), [])

    def test_cut_and_collapsed(self):
        self.assertIn("cut", self.rules(_entry((0, 0, 9, 9), None, text="ab…",
                                               source="abcdef", cut=False)))
        self.assertIn("collapsed", self.rules(_entry((0, 0, 0, 0), None,
                                                     text="", source="abc")))


class TestLedger(unittest.TestCase):
    def draw(self, fn):
        img = Image.new("RGB", (400, 100))
        with textbox.recording() as ledger:
            fn(ImageDraw.Draw(img))
        return ledger

    def test_not_recording_is_inert(self):
        ImageDraw.Draw(Image.new("RGB", (50, 50))).text((0, 0), "hi")
        self.assertFalse(textbox.active())

    def test_records_measured_ink_and_box(self):
        def go(d):
            textbox.note("hello world", (0, 0, 20, 20), label="t")
            d.text((0, 0), "hello world")
        (entry,) = self.draw(go)
        self.assertEqual(entry["label"], "t")
        self.assertTrue(textbox.violations(entry))

    def test_put_clips_with_ellipsis_and_stays_in_room(self):
        out = []

        def go(d):
            out.append(ui_line.put(d, (0, 0), "w" * 80, None, (255,) * 3,
                                   room=100))
        (entry,) = self.draw(go)
        self.assertTrue(out[0].endswith(ui_line.ELLIPSIS))
        self.assertEqual(textbox.violations(entry), [])

    def test_empty_string_draws_nothing(self):
        self.assertEqual(self.draw(lambda d: ui_line.put(
            d, (0, 0), "", None, (1, 1, 1), room=50)), [])


def _overflow_module():
    def run(screen, params, stop):
        img = screen.new_image((0, 0, 0))
        title = "A title far too long for the box it was declared to stay in"
        textbox.note(title, (100, 100, 400, 160), label="demo title")
        ImageDraw.Draw(img).text((100, 100), title,
                                 font=ui_text.face(screen, 40))
        screen.present(img)
    return types.SimpleNamespace(run=run, PARAMS={}, NAME="demo")


@unittest.skipUnless(HAVE_FONT, "no DejaVu font on this machine")
class TestProbe(unittest.TestCase):
    def test_deliberate_overflow_is_reported(self):
        res = text_audit.check_case(_overflow_module(), {"label": "demo"},
                                    1920, 1080)
        self.assertFalse(res["ok"])
        hit = res["offences"][0]
        self.assertEqual((hit["rule"], hit["label"]), ("wider", "demo title"))
        self.assertIn("far too long", hit["text"])
        self.assertEqual(hit["box"], [100, 100, 400, 160])

    def test_real_cases_stay_clean(self):
        mods = renderer_registry.load_renderers(renderer_registry.RENDERER_DIR)
        for name in ("notice", "text", "macbook", "sleep"):
            for case in text_stress.cases_for(name):
                res = text_audit.check_case(mods[name]["module"], case,
                                            1920, 1080)
                self.assertTrue(res["ok"], (name, case["label"], res))

    def test_stress_covers_the_required_shapes(self):
        labels = {c["label"] for c in text_stress.cases_for("notice")}
        self.assertTrue({"long title", "empty", "non-ascii"} <= labels)
        macbook = " ".join(c["label"] for c in text_stress.cases_for("macbook"))
        self.assertIn("long app", macbook)


@unittest.skipUnless(HAVE_FONT, "no DejaVu font on this machine")
class TestEndpoint(unittest.TestCase):
    def setUp(self):
        import displayd
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.addCleanup(lambda: os.environ.pop("DISPLAYD_FAKE_FB", None)
                        if self._env is None
                        else os.environ.__setitem__("DISPLAYD_FAKE_FB",
                                                    self._env))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(tmp.name, "p.json"),
            feedback_path=os.path.join(tmp.name, "f.jsonl"))
        self.addCleanup(self.daemon.clear)

    def test_report_is_per_view_and_clean(self):
        report = self.daemon.text_check()
        self.assertTrue(report["ok"], [(n, v["offences"]) for n, v in
                                       report["views"].items()
                                       if v.get("offences")])
        self.assertIn("notice", report["views"])
        for name in text_audit.NEEDS_ENGINE:
            if name in report["views"] and not report["engine"]:
                self.assertFalse(report["views"][name]["checkable"])

    def test_overflowing_view_turns_the_report_red(self):
        self.daemon.renderers["zz_demo"] = {"module": _overflow_module()}
        report = self.daemon.text_check()
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "overflow")
        self.assertFalse(report["views"]["zz_demo"]["ok"])
        self.assertEqual(report["views"]["zz_demo"]["offences"][0]["rule"],
                         "wider")


if __name__ == "__main__":
    unittest.main()
