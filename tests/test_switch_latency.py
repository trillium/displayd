"""Switch-latency gate: every registry view, request -> first pixel < 100 ms.

The sweep itself is ``tools/switch_latency.py``; this test asserts on its
report. Views that cannot be measured here are named in the output (never
silently skipped), and an empty measured set fails.

Run from the repo root:  python3 -m unittest tests.test_switch_latency -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "tools"))

import switch_latency as gate  # noqa: E402
import switch_latency_fixtures as fixtures  # noqa: E402


class TestSwitchLatency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lines = []
        cls.report = gate.run(out=lines.append)
        cls.text = "\n".join(lines)

    def test_every_registry_view_has_a_fixture(self):
        # Fixtures name the registry exactly; a new view must enter the gate.
        import displayd
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            daemon = displayd.DisplayDaemon(
                policy_path=os.path.join(tmp, "p.json"),
                feedback_path=os.path.join(tmp, "f.jsonl"))
        offered = {n for n, e in daemon.renderers.items() if "module" in e}
        self.assertEqual(offered - set(fixtures.VIEWS), set())

    def test_gate_is_green_and_names_everything(self):
        self.assertEqual(self.report["problems"], [], self.text)
        self.assertTrue(self.report["measured"], "empty gate is a failure")
        for row in self.report["measured"]:
            self.assertLess(row["ms"], gate.BUDGET_MS, row)

    def test_unmeasured_and_slow_views_are_named_in_the_output(self):
        for row in self.report["not_measured"] + self.report["known_slow"]:
            self.assertIn(row["view"], self.text)

    def test_every_view_is_accounted_for_exactly_once(self):
        seen = [r["view"] for r in self.report["measured"]
                + self.report["not_measured"] + self.report["known_slow"]]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), set(fixtures.VIEWS))


class TestGateCanFail(unittest.TestCase):
    def test_a_slow_view_fails_by_name(self):
        # Budget of 0.01 ms: every real view is "deliberately slow".
        report = gate.run(budget=0.01, only={"clock"}, out=None)
        self.assertTrue(any(p.startswith("clock:") and "budget" in p
                            for p in report["problems"]), report)

    def test_a_view_without_a_fixture_fails(self):
        saved = fixtures.VIEWS.pop("clock")
        try:
            report = gate.run(only={"clock"}, out=None)
        finally:
            fixtures.VIEWS["clock"] = saved
        self.assertTrue(any("no fixture" in p for p in report["problems"]))

    def test_an_empty_measured_set_fails(self):
        report = gate.run(only={"no-such-view"}, out=None)
        self.assertTrue(any("empty gate" in p for p in report["problems"]))


if __name__ == "__main__":
    unittest.main()
