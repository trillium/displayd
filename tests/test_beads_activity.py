"""Beads activity bridge + renderer (bridges/beads_activity.py,
renderers/activity.py) tests.

Proves the observational contract at three levels: the bridge's
parsing/filtering and refusal paths against scripted upstream payloads,
the bridge-to-daemon schema contract (every normalized event validates
against the renderer's real INPUTS spec), and a live push through the
real activity renderer into a headless daemon feed.

Run from the repo root:  python3 -m unittest tests.test_beads_activity -v
"""

import json
import logging
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))

import displayd
from displayd import FeedStore, HeadlessFramebuffer, Screen, validate_value

import beads_activity
from beads_activity import ActivityBridge, normalize_event


def good_event(seq=1, **kw):
    ev = {"seq": seq, "at": "2026-09-28T12:00:00.000Z", "tool": "bead_show",
          "outcome": "ok", "caller": "loopback-local", "sessionId": None,
          "client": "pi", "authed": False, "durationMs": 42,
          "argNames": ["id"], "beadRefs": ["task-2nwlw"],
          "summary": "did the thing"}
    ev.update(kw)
    return ev


def make_bridge(events=None, fail_fetch=False, **kw):
    """Bridge with a scripted upstream and capturing post fn."""
    posted = []
    events = list(events) if events is not None else []

    def fetch(url):
        if fail_fetch:
            raise ConnectionError("upstream down")
        return {"events": list(events)}

    kw.setdefault("bridge_base", "http://bridge:3737")
    kw.setdefault("displayd_base", "http://displayd:8980")
    kw.setdefault("fetch_fn", fetch)
    kw.setdefault("post_fn", posted.append)
    return ActivityBridge(**kw), posted, events


class TestNormalize(unittest.TestCase):
    def test_good_event_passes_through(self):
        out = normalize_event(good_event())
        self.assertEqual(out["tool"], "bead_show")
        self.assertEqual(out["outcome"], "ok")
        self.assertEqual(out["seq"], 1)
        self.assertEqual(out["beadRefs"], ["task-2nwlw"])
        self.assertEqual(out["summary"], "did the thing")

    def test_error_outcome_passes(self):
        out = normalize_event(good_event(outcome="error"))
        self.assertEqual(out["outcome"], "error")

    def test_refusals_never_raise(self):
        for raw in (None, "junk", 42, [], {"tool": "", "outcome": "ok"},
                    {"tool": "x"}, {"outcome": "ok"},
                    {"tool": "bead_show", "outcome": "weird"},
                    {"tool": "bead_show", "outcome": "OK"},
                    {"tool": "bead_show", "outcome": None}):
            self.assertIsNone(normalize_event(raw),
                              "expected refusal for %r" % (raw,))

    def test_summary_truncated_to_cap(self):
        out = normalize_event(good_event(summary="s" * 5000))
        self.assertEqual(len(out["summary"]), beads_activity.SUMMARY_CHARS)

    def test_arg_names_capped(self):
        out = normalize_event(good_event(argNames=["a" * 200] * 60))
        self.assertEqual(len(out["argNames"]), beads_activity.MAX_ARG_NAMES)
        self.assertTrue(all(len(a) <= beads_activity.ARG_NAME_LEN
                            for a in out["argNames"]))

    def test_bead_refs_validated_not_just_shaped(self):
        out = normalize_event(good_event(
            beadRefs=["task-2nwlw", "follow-on", "end-to-end", "!!!",
                      "brain-ow1w.1.2"]))
        self.assertEqual(out["beadRefs"], ["task-2nwlw", "brain-ow1w.1.2"])

    def test_bead_ref_count_capped(self):
        refs = ["task-%04d" % i for i in range(40)]
        out = normalize_event(good_event(beadRefs=refs))
        self.assertEqual(len(out["beadRefs"]), beads_activity.MAX_BEADREFS)

    def test_bad_types_coerced_not_fatal(self):
        out = normalize_event(good_event(seq="junk", durationMs="fast",
                                         authed="yes", caller=["x"],
                                         sessionId=123))
        self.assertEqual(out["tool"], "bead_show")
        self.assertNotIn("seq", out)  # absent stays absent
        self.assertEqual(out["durationMs"], 0)
        self.assertTrue(out["authed"])

    def test_duration_floored(self):
        out = normalize_event(good_event(durationMs=-50))
        self.assertEqual(out["durationMs"], 0)


class TestPoll(unittest.TestCase):
    def test_first_poll_forwards_backfill_only(self):
        bridge, posted, _ = make_bridge([good_event(s) for s in range(1, 21)],
                                        backfill=8)
        with self.assertLogs("beads-activity-bridge", level="INFO"):
            n, outcome = bridge.poll_once()
        self.assertEqual(outcome, "ok")
        self.assertEqual(n, 8)
        self.assertEqual([p["seq"] for p in posted], list(range(13, 21)))

    def test_second_poll_forwards_only_new(self):
        bridge, posted, upstream = make_bridge(
            [good_event(s) for s in range(1, 6)])
        with self.assertLogs("beads-activity-bridge", level="INFO"):
            bridge.poll_once()
        posted.clear()
        upstream.append(good_event(6))
        n, _ = bridge.poll_once()
        self.assertEqual(n, 1)
        self.assertEqual(posted[0]["seq"], 6)
        n, _ = bridge.poll_once()  # steady state: nothing new
        self.assertEqual(n, 0)

    def test_malformed_events_dropped_never_forwarded(self):
        bridge, posted, _ = make_bridge(
            [good_event(1), {"tool": "", "outcome": "ok"},
             {"tool": "x", "outcome": "bogus"}, "junk"])
        with self.assertLogs("beads-activity-bridge", level="INFO"):
            n, outcome = bridge.poll_once()
        self.assertEqual(outcome, "ok")
        self.assertEqual(n, 1)
        self.assertEqual(posted[0]["seq"], 1)
        self.assertEqual(bridge.dropped, 3)

    def test_forward_order_is_completion_order(self):
        bridge, posted, _ = make_bridge(
            [good_event(3), good_event(1), good_event(2)], backfill=3)
        with self.assertLogs("beads-activity-bridge", level="INFO"):
            bridge.poll_once()
        self.assertEqual([p["seq"] for p in posted], [1, 2, 3])

    def test_seq_reset_reseeds_then_backfills(self):
        bridge, posted, upstream = make_bridge(
            [good_event(s) for s in (100, 101)])
        with self.assertLogs("beads-activity-bridge", level="INFO"):
            bridge.poll_once()
        posted.clear()
        # Upstream restarted: seq rewound, ring holds new-process history.
        upstream[:] = [good_event(s) for s in (1, 2, 3)]
        with self.assertLogs("beads-activity-bridge", level="INFO") as logs:
            n, _ = bridge.poll_once()
        self.assertIn("seq reset", "\n".join(logs.output))
        # Reseed + first-poll path: the new history backfills once...
        self.assertEqual(n, 3)
        self.assertEqual([p["seq"] for p in posted], [1, 2, 3])
        # ...and a steady-state repeat forwards nothing.
        posted.clear()
        n, _ = bridge.poll_once()
        self.assertEqual(n, 0)
        self.assertEqual(posted, [])

    def test_upstream_down_is_reconnect_never_death(self):
        bridge, posted, _ = make_bridge(fail_fetch=True)
        with self.assertLogs("beads-activity-bridge", level="INFO"):
            n, outcome = bridge.poll_once()
        self.assertEqual((n, outcome), (0, "upstream-down"))
        self.assertEqual(posted, [])

    def test_displayd_down_drops_but_survives(self):
        def down(payload):
            raise ConnectionError("panel away")

        bridge = ActivityBridge("http://bridge:3737", "http://displayd:8980",
                                fetch_fn=lambda u: {"events": [good_event(1)]},
                                post_fn=down)
        with self.assertLogs("beads-activity-bridge", level="INFO"):
            n, outcome = bridge.poll_once()
        self.assertEqual(outcome, "ok")
        self.assertEqual(n, 0)  # nothing delivered, bridge still alive

    def test_config_errors_raise(self):
        with self.assertRaises(ValueError):
            ActivityBridge("", "http://displayd:8980")
        with self.assertRaises(ValueError):
            ActivityBridge("http://bridge:3737", "")
        with self.assertRaises(ValueError):
            ActivityBridge("http://bridge:3737", "http://displayd:8980",
                           interval=0.1)


class TestContract(unittest.TestCase):
    """Every normalized event must pass the daemon's real validation for
    the renderer's real INPUTS spec -- the bridge owns the protocol, the
    daemon only ever sees validated payloads."""

    @classmethod
    def setUpClass(cls):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        assert "activity" in found, "activity renderer not advertised"
        cls.spec = found["activity"]["inputs"]["event"]

    def test_inputs_advertised(self):
        self.assertIn("tool", self.spec["required"])
        self.assertIn("outcome", self.spec["required"])

    def test_normalized_events_validate(self):
        for raw in (good_event(),
                    good_event(outcome="error", sessionId="abc",
                               beadRefs=[], summary="x" * 5000),
                    good_event(seq=None, at=None, caller=None)):
            validate_value(normalize_event(raw), self.spec,
                           "activity.event")

    def test_unvalidated_upstream_does_not_validate(self):
        with self.assertRaises(ValueError):
            validate_value({"tool": "x"}, self.spec, "activity.event")


class TestRendererLive(unittest.TestCase):
    def test_events_reach_real_renderer(self):
        screen = Screen(HeadlessFramebuffer(width=640, height=360))
        screen.feeds = FeedStore()
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        activity = found["activity"]["module"]
        spec = found["activity"]["inputs"]["event"]
        for s in (1, 2, 3):
            screen.feeds.push("activity", "event",
                              normalize_event(good_event(s)), spec)
        stop = threading.Event()
        runner = threading.Thread(target=activity.run,
                                  args=(screen, {}, stop), daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while screen.fb.last_frame is None and time.monotonic() < deadline:
            time.sleep(0.05)
        stop.set()
        runner.join(5)
        self.assertIsNotNone(screen.fb.last_frame,
                             "renderer never presented the pushed events")

    def test_cold_renderer_presents_waiting_frame(self):
        screen = Screen(HeadlessFramebuffer(width=640, height=360))
        screen.feeds = FeedStore()
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        activity = found["activity"]["module"]
        stop = threading.Event()
        runner = threading.Thread(target=activity.run,
                                  args=(screen, {}, stop), daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while screen.fb.last_frame is None and time.monotonic() < deadline:
            time.sleep(0.05)
        stop.set()
        runner.join(5)
        self.assertIsNotNone(screen.fb.last_frame,
                             "cold renderer left the panel blank")


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
