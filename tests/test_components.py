"""The component layer and its structural gate.

Two things are pinned here:

1. ``tools/check-components.py`` -- the ratchet that fails while a shipped
   module draws outside ``renderers/ui/``. The gate is only useful if it
   fails in both directions (a new hand-drawing file, and a stale
   exemption), so those behaviours are exercised on a synthetic tree.
2. ``renderers/ui/`` -- the composition guarantee (a failing layer is
   skipped, never blanking the panel) and the coupling between what a
   badge DRAWS and what the touch audit says is there.

Run from the repo root:  python3 -m unittest tests.test_components -v
"""

import contextlib
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

from PIL import Image

from ui import base, system_buttons as buttons

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "check-components.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("displayd_check_components",
                                                  TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_tree(files):
    """A throwaway repo root holding just the given relative files."""
    tmp = tempfile.TemporaryDirectory()
    root = pathlib.Path(tmp.name)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp, root


def run_gate(mod, argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = mod.main(argv)
    return rc, out.getvalue()


class GateTest(unittest.TestCase):
    """The structural gate: fails both ways, names the offenders."""

    DRAWS = "from PIL import ImageDraw\nprint(ImageDraw)\n"

    def setUp(self):
        self.gate = load_gate()
        self._exempt = self.gate.EXEMPTIONS
        self.addCleanup(self._restore)

    def _restore(self):
        self.gate.EXEMPTIONS = self._exempt

    def test_the_real_repo_satisfies_the_ratchet(self):
        rc, out = run_gate(self.gate, [])
        self.assertEqual(rc, 0, out)
        self.assertIn("component layer ok", out)

    def test_a_new_hand_drawing_file_fails_and_is_named(self):
        tmp, root = make_tree({"renderers/synthetic_view.py": self.DRAWS})
        self.addCleanup(tmp.cleanup)
        self.gate.EXEMPTIONS = ()
        rc, out = run_gate(self.gate, ["--root", str(root)])
        self.assertEqual(rc, 1)
        self.assertIn("renderers/synthetic_view.py", out)
        self.assertIn("outside the component layer", out)

    def test_a_stale_exemption_fails(self):
        tmp, root = make_tree({"renderers/migrated.py": "print('clean')\n"})
        self.addCleanup(tmp.cleanup)
        self.gate.EXEMPTIONS = ("renderers/migrated.py",)
        rc, out = run_gate(self.gate, ["--root", str(root)])
        self.assertEqual(rc, 1)
        self.assertIn("stale exemption", out)
        self.assertIn("renderers/migrated.py", out)

    def test_the_component_layer_may_draw(self):
        tmp, root = make_tree({"renderers/ui/widget.py": self.DRAWS})
        self.addCleanup(tmp.cleanup)
        self.gate.EXEMPTIONS = ()
        rc, out = run_gate(self.gate, ["--root", str(root)])
        self.assertEqual(rc, 0, out)

    def test_a_root_level_panel_module_is_in_scope(self):
        tmp, root = make_tree({"progress_bar.py": self.DRAWS})
        self.addCleanup(tmp.cleanup)
        self.gate.EXEMPTIONS = ()
        rc, out = run_gate(self.gate, ["--root", str(root)])
        self.assertEqual(rc, 1)
        self.assertIn("progress_bar.py", out)

    def test_out_of_scope_directories_are_ignored(self):
        tmp, root = make_tree({"tools/helper.py": self.DRAWS,
                               "tests/test_x.py": self.DRAWS,
                               "bridges/mac.py": self.DRAWS})
        self.addCleanup(tmp.cleanup)
        self.gate.EXEMPTIONS = ()
        rc, out = run_gate(self.gate, ["--root", str(root)])
        self.assertEqual(rc, 0, out)

    def test_list_prints_the_offenders_sorted(self):
        tmp, root = make_tree({"renderers/b.py": self.DRAWS,
                               "a.py": self.DRAWS})
        self.addCleanup(tmp.cleanup)
        rc, out = run_gate(self.gate, ["--root", str(root), "--list"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.split(), ["a.py", "renderers/b.py"])


class ChainTest(unittest.TestCase):
    """The promoted safety property: a failing component is skipped."""

    @staticmethod
    def black(side=64):
        return Image.new("RGB", (side, side), (0, 0, 0))

    def test_applies_in_order(self):
        calls = []

        def first(img):
            calls.append("first")
            return img

        def second(img):
            calls.append("second")
            return img

        base.chain(first, second)(self.black())
        self.assertEqual(calls, ["first", "second"])

    def test_a_failing_layer_never_blanks_the_panel(self):
        def bad(img):
            raise RuntimeError("SYNTHETIC component failure")

        def good(img):
            img.putpixel((5, 5), (255, 255, 255))
            return img

        out = base.chain(bad, good)(self.black())
        self.assertEqual(out.getpixel((5, 5)), (255, 255, 255))

    def test_none_entries_and_none_returns_keep_the_frame(self):
        def nothing(img):
            return None

        img = self.black()
        self.assertIs(base.chain(nothing)(img), img)
        self.assertIs(base.chain(None, None)(img), img)


class SystemButtonsTest(unittest.TestCase):
    """One definition, two buttons, and no lie between draw and audit."""

    VIEWS = ("clock", "picker", "unified", "reload", "notice", "sleep")

    def overlay(self, view):
        screen = types.SimpleNamespace(current_view=view)
        return buttons.system_overlay(screen, None)

    def lit(self, img, rect):
        x, y, w, h = rect
        return any(img.getpixel((px, py)) != (0, 0, 0)
                   for px in range(x, x + w, 7)
                   for py in range(y, y + h, 7))

    def test_the_two_old_modules_are_gone(self):
        self.assertFalse((ROOT / "renderers" / "home_chrome.py").exists())
        self.assertFalse((ROOT / "renderers" / "sleep_chrome.py").exists())

    def test_both_buttons_share_one_geometry_rule(self):
        # One strip width and one clamp for the pair: a badge is a square
        # inside a gesture strip, whichever corner it sits in.
        self.assertEqual(buttons.STRIP, 160)
        self.assertEqual(buttons.home_rect(160, 90)[2:],
                         buttons.sleep_rect(160, 90)[2:])

    def test_audit_exact_matches_what_is_drawn(self):
        for view in self.VIEWS:
            with self.subTest(view=view):
                img = self.overlay(view)(
                    Image.new("RGB", (1920, 1080), (0, 0, 0)))
                drawn = []
                for name, rect in (("home", buttons.home_rect(1920, 1080)),
                                   ("screen-off",
                                    buttons.sleep_rect(1920, 1080))):
                    if self.lit(img, rect):
                        drawn.append(name)
                audited = [e["id"] for e in buttons.audit_exact(view)]
                self.assertEqual(drawn, audited)

    def test_the_base_layer_survives_the_buttons(self):
        img = Image.new("RGB", (1920, 1080), (0, 0, 0))

        def bar(frame):
            frame.putpixel((900, 1075), (255, 0, 0))
            return frame

        screen = types.SimpleNamespace(current_view="clock")
        out = buttons.system_overlay(screen, base.chain(bar))(img)
        self.assertEqual(out.getpixel((900, 1075)), (255, 0, 0))
        self.assertTrue(self.lit(out, buttons.home_rect(1920, 1080)))

    def test_a_broken_button_cannot_blank_the_panel(self):
        original = buttons.draw_sleep_button
        buttons.draw_sleep_button = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("SYNTHETIC button failure"))
        self.addCleanup(setattr, buttons, "draw_sleep_button", original)

        def bar(frame):
            frame.putpixel((900, 1075), (255, 0, 0))
            return frame

        screen = types.SimpleNamespace(current_view="clock")
        out = buttons.system_overlay(screen, base.chain(bar))(
            Image.new("RGB", (1920, 1080), (0, 0, 0)))
        self.assertEqual(out.getpixel((900, 1075)), (255, 0, 0))
        self.assertTrue(self.lit(out, buttons.home_rect(1920, 1080)))


if __name__ == "__main__":
    unittest.main()
