"""Per-app window placement tests (bridges/talon_windows.py).

Pure mapping (place/containing) plus a no-raise smoke check of the
live Quartz snapshot. No clicks, no focus changes, no feed writes.

Run from the repo root:  python3 -m unittest tests.test_talon_windows -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))

import talon_windows as tw

DISPLAYS = [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
             "main": True},
            {"bounds": {"x": -1692, "y": -135, "w": 1692, "h": 945},
             "main": False}]


class ContainingTest(unittest.TestCase):
    def test_each_display_found(self):
        self.assertEqual(tw.containing(100, 100, DISPLAYS), 0)
        self.assertEqual(tw.containing(-1000, 0, DISPLAYS), 1)

    def test_outside_is_none(self):
        self.assertIsNone(tw.containing(5000, 5000, DISPLAYS))
        self.assertIsNone(tw.containing(0, 0, []))
        self.assertIsNone(tw.containing("x", 0, DISPLAYS))


class PlaceTest(unittest.TestCase):
    def test_exact_then_casefold_match(self):
        owners = {"Code": (100, 100), "finder": (200, 200)}
        out = tw.place(["Code", "Finder", "Ghost"], owners, DISPLAYS)
        self.assertEqual(out["Code"], {"x": 100, "y": 100, "d": 0})
        self.assertEqual(out["Finder"]["d"], 0)
        self.assertNotIn("Ghost", out)  # windowless: no entry

    def test_dict_points_and_outside_display(self):
        owners = {"Far": {"x": 9000, "y": 9000}}
        out = tw.place(["Far"], owners, DISPLAYS)
        self.assertEqual(out["Far"]["d"], None)

    def test_only_listed_apps_bounded_ints(self):
        owners = {"A": (10.7, 20.2), "B": (30, 40), "C": (50, 60)}
        out = tw.place(["A", "B"], owners, DISPLAYS)
        self.assertEqual(sorted(out), ["A", "B"])
        self.assertEqual(out["A"], {"x": 10, "y": 20, "d": 0})

    def test_garbage_never_raises(self):
        self.assertEqual(tw.place(None, None, None), {})
        self.assertEqual(tw.place("nope", {"A": 1}, []), {})
        self.assertEqual(tw.place([None, 42], None, DISPLAYS), {})
        self.assertEqual(tw.place(["A"], {"A": "junk"}, DISPLAYS), {})


class SnapshotTest(unittest.TestCase):
    def test_snapshot_never_raises_and_shaped(self):
        owners, displays = tw.snapshot()
        # Without Quartz (CI/Linux): (None, None). With Quartz (Mac):
        # dict + list, or (None, None) on any failure. Never raises,
        # never a half-shaped pair.
        if owners is None or displays is None:
            self.assertEqual((owners, displays), (None, None))
        else:
            self.assertIsInstance(owners, dict)
            self.assertIsInstance(displays, list)
            for d in displays:
                self.assertIn("bounds", d)
                self.assertIn("main", d)


if __name__ == "__main__":
    unittest.main()
