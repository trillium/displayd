"""The template trust gate must pass today's tree and fail on bad ones.

A check that cannot fail proves nothing, so each rule is exercised on a
synthetic template root as well as on the real ``html-templates/``.

Run from the repo root:  python3 -m unittest tests.test_template_trust -v
"""

import contextlib
import io
import os
import pathlib
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "tools"))

import check_template_trust as gate  # noqa: E402

GOOD = "<html><body>{{title}}{{tiles|raw}}{{names|raw}}{{panes|raw}}" \
       "</body></html>"


def run_gate(files):
    """Run the gate over a synthetic root; returns (rc, output)."""
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in files.items():
            pathlib.Path(tmp, name).write_text(text, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gate.main(["--root", tmp])
    return rc, out.getvalue()


class TestRealTree(unittest.TestCase):
    def test_every_shipped_template_passes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = gate.main([])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("frozen at %d" % gate.RAW_SLOT_COUNT, out.getvalue())


class TestRule1(unittest.TestCase):
    def test_clean_root_passes(self):
        self.assertEqual(run_gate({"a.html": GOOD})[0], 0)

    def test_each_active_or_remote_form_fails_by_name(self):
        cases = {
            "no-script-in-a-template": "<script>x()</script>",
            "no-javascript-url-in-a-template":
                '<a href="javascript:x()">a</a>',
            "no-at-import-in-a-template":
                "<style>@import url(x.css);</style>",
            "no-remote-resource-in-a-template":
                '<img src="https://evil.invalid/a.png">',
        }
        for rule, bad in cases.items():
            with self.subTest(rule=rule):
                rc, out = run_gate({"a.html": GOOD + bad})
                self.assertEqual(rc, 1)
                self.assertIn(rule, out)
                self.assertIn("a.html", out)

    def test_prose_in_a_comment_is_not_a_finding(self):
        text = GOOD + "<!-- no @import, no https://x, no javascript: -->"
        self.assertEqual(run_gate({"a.html": text})[0], 0)

    def test_commented_out_script_still_fails(self):
        rc, out = run_gate({"a.html": GOOD + "<!-- <script>x</script> -->"})
        self.assertEqual(rc, 1)
        self.assertIn("no-script-in-a-template", out)


class TestRule2(unittest.TestCase):
    def test_extra_raw_slot_fails(self):
        rc, out = run_gate({"a.html": GOOD + "{{more|raw}}"})
        self.assertEqual(rc, 1)
        self.assertIn("raw-slot-count-is-frozen", out)

    def test_fewer_raw_slots_also_fails(self):
        rc, out = run_gate({"a.html": "<p>{{x|raw}}</p>"})
        self.assertEqual(rc, 1)
        self.assertIn("raw-slot-count-is-frozen", out)


if __name__ == "__main__":
    unittest.main()
