"""Pure tick-planning rules behind the live preview bridge.

mac_preview_plan owns the three decisions that set frame age and wire
volume -- round-robin stagger, last-known merge, and the byte-identical
skip -- so they can be pinned here with no Mac, no ffmpeg, no network.

Run from the repo root:  python3 -m unittest tests.test_mac_preview_plan -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

import mac_preview_plan as plan


def frame(index, jpeg="abc", w=96, h=60):
    return {"display_index": index, "w": w, "h": h, "jpeg": jpeg}


def run_ticks(period, seconds, budget=plan.FRESH_BUDGET):
    """POST timeline of a STATIC screen at a fixed tick period.

    Mirrors the loop's use of the rules exactly: a byte-identical set
    skips unless may_skip_post says the next tick would overrun the
    budget. Returns (post_times, skips) over `seconds` of virtual time."""
    sig = plan.frame_signature([frame(0), frame(1)])
    posts, skips = [0.0], 0
    last_post, now = 0.0, period
    while now < seconds:
        if plan.may_skip_post(sig, sig, now, last_post, period, budget):
            skips += 1
        else:
            posts.append(now)
            last_post = now
        now += period
    return posts, skips


class StaggerCase(unittest.TestCase):
    def test_round_robin_is_out_of_phase(self):
        self.assertEqual([plan.next_display_index(t, 2) for t in range(6)],
                         [0, 1, 0, 1, 0, 1])
        self.assertEqual([plan.next_display_index(t, 1) for t in range(3)],
                         [0, 0, 0])

    def test_no_displays_or_garbage_is_none(self):
        for bad in (0, -1, None):
            self.assertIsNone(plan.next_display_index(0, bad), bad)
        self.assertIsNone(plan.next_display_index(0, "x"))
        self.assertEqual(plan.next_display_index("3", 2), 1)

    def test_exactly_one_capture_per_tick(self):
        # The stagger contract: index count == tick count, never two.
        seen = [plan.next_display_index(t, 2) for t in range(10)]
        self.assertEqual(len(seen), 10)
        self.assertEqual(sorted(set(seen)), [0, 1])


class MergeCase(unittest.TestCase):
    def test_fresh_frame_joins_last_known(self):
        one = {0: frame(0)}
        both = plan.merge_frames(one, 1, frame(1), 2)
        self.assertEqual(sorted(both), [0, 1])
        self.assertEqual(plan.frame_list(both), [frame(0), frame(1)])
        self.assertEqual(sorted(one), [0])  # input never mutated

    def test_failed_capture_drops_that_display(self):
        both = {0: frame(0), 1: frame(1)}
        dropped = plan.merge_frames(both, 1, None, 2)
        self.assertEqual(sorted(dropped), [0])  # box, never a stale photo

    def test_disconnected_display_is_dropped(self):
        both = {0: frame(0), 1: frame(1)}
        self.assertEqual(sorted(plan.merge_frames(both, 0, frame(0), 1)),
                         [0])

    def test_garbage_inputs_never_raise(self):
        self.assertEqual(plan.merge_frames(None, 0, frame(0), 1), {0: frame(0)})
        self.assertEqual(sorted(plan.merge_frames({0: frame(0)}, "x",
                                                  frame(1))), [0])
        self.assertEqual(plan.merge_frames({0: frame(0)}, 0, frame(0),
                                           None), {0: frame(0)})
        self.assertEqual(plan.frame_list(None), [])
        self.assertEqual(plan.frame_list({"nope": 1}), [])


class SignatureCase(unittest.TestCase):
    def test_identical_content_matches_regardless_of_order(self):
        a = plan.frame_signature([frame(0), frame(1)])
        b = plan.frame_signature([frame(1), frame(0)])
        self.assertTrue(a)
        self.assertEqual(a, b)

    def test_changed_bytes_change_signature(self):
        a = plan.frame_signature([frame(0, "abc")])
        b = plan.frame_signature([frame(0, "abd")])
        self.assertNotEqual(a, b)

    def test_empty_and_garbage_are_blank(self):
        self.assertEqual(plan.frame_signature([]), "")
        self.assertEqual(plan.frame_signature(None), "")
        self.assertEqual(plan.frame_signature([{"jpeg": "x"}]), "")
        self.assertEqual(plan.frame_signature("nope"), "")

    def test_signature_ignores_ts_and_extra_keys(self):
        # ts is deliberately outside the signature: it changes every POST.
        a = {"ts": 1.0, "frames": [frame(0)]}
        b = {"ts": 9.0, "frames": [dict(frame(0), stuff="x")]}
        self.assertEqual(plan.frame_signature(a["frames"]),
                         plan.frame_signature(b["frames"]))


class SkipCase(unittest.TestCase):
    def test_changed_or_first_set_always_posts(self):
        sig = plan.frame_signature([frame(0)])
        # No baseline yet (first POST): skip would mean never posting.
        self.assertFalse(plan.may_skip_post(sig, "", 0.0, 0.0, 1.0))
        # Changed bytes: the panel must hear about it.
        other = plan.frame_signature([frame(0, "zzz")])
        self.assertFalse(plan.may_skip_post(other, sig, 1.0, 0.0, 1.0))
        # Nothing postable: "" is never skipped into a POST either.
        self.assertFalse(plan.may_skip_post("", "", 1.0, 0.0, 1.0))

    def test_identical_set_skips_only_while_fresh(self):
        sig = plan.frame_signature([frame(0)])
        # Room for the tick after this one inside the budget: skip.
        self.assertTrue(plan.may_skip_post(sig, sig, 0.5, 0.0, 0.5))
        # The tick after this one would overrun the budget: POST.
        self.assertFalse(plan.may_skip_post(sig, sig, 2.0, 0.0, 1.0))

    def test_garbage_always_posts(self):
        self.assertFalse(plan.may_skip_post("x", "x", None, 0.0, 1.0))
        self.assertFalse(plan.may_skip_post("x", "x", 1.0, None, 1.0))

    def test_budget_is_inside_the_renderer_freshness_window(self):
        # The renderer's own constant is the contract; guard the drift.
        import macbook_preview as renderer
        self.assertLess(plan.FRESH_BUDGET, renderer.PREVIEW_FRESH)

    def test_no_skip_window_ever_ages_the_feed_stale(self):
        # Across tick periods on this hardware scale (~0.4-1.7 s capture
        # wall + wait): every POST gap must stay under the budget, both
        # in the regimes that CAN skip and the measured one that cannot.
        # Periods sit clear of the knife edge (2 * period == budget),
        # where the guard's float comparison is deliberately ambiguous.
        for period in (0.6, 1.0, 1.3, 1.5, 1.7, 2.0):
            posts, skips = run_ticks(period, seconds=60.0)
            gaps = [b - a for a, b in zip(posts, posts[1:])]
            self.assertTrue(gaps, period)
            self.assertLess(max(gaps), plan.FRESH_BUDGET, period)
            if period <= 1.3:
                self.assertGreater(skips, 0, period)  # wire really skipped
            if period >= 1.5:
                self.assertEqual(skips, 0, period)  # safety first, no skips


if __name__ == "__main__":
    unittest.main()
