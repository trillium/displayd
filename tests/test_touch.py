"""Touch-input tests: parser, normalization, lifecycle, hit test, dispatch.

Deterministic: synthetic evdev records via touch.pack_event, no hardware.

Run from the repo root:  python3 -m unittest tests.test_touch -v
"""

import io
import json
import os
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import touch
from touch import (
    ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_SLOT, ABS_MT_TRACKING_ID,
    ABS_X, ABS_Y, BTN_TOUCH, ACTION_TABLE, CONFIDENCE_DEFAULTS, EV_ABS,
    EV_KEY, EV_SYN, SYN_REPORT, DisplaydClient, EvdevParser, TapDetector,
    TouchEvent, TouchService, action_request, confidence_env_override,
    default_config, endpoint_allowed, hit_test, load_config, normalize,
    normalize_confidence_feedback, pack_event, parse_event, resolve_action,
)


def mt_down(slot, tid, x, y):
    return [(EV_ABS, ABS_MT_SLOT, slot),
            (EV_ABS, ABS_MT_TRACKING_ID, tid),
            (EV_ABS, ABS_MT_POSITION_X, x),
            (EV_ABS, ABS_MT_POSITION_Y, y)]


def mt_up(slot):
    return [(EV_ABS, ABS_MT_SLOT, slot),
            (EV_ABS, ABS_MT_TRACKING_ID, -1)]


def st_down(x, y):
    return [(EV_ABS, ABS_X, x), (EV_ABS, ABS_Y, y),
            (EV_KEY, BTN_TOUCH, 1)]


def st_up():
    return [(EV_KEY, BTN_TOUCH, 0)]


CAL = {"x_min": 0, "x_max": 4095, "y_min": 0, "y_max": 4095}


class ParseTest(unittest.TestCase):
    def test_pack_parse_roundtrip(self):
        rec = pack_event(EV_ABS, ABS_X, 1234)
        self.assertEqual(len(rec), touch.EVENT_SIZE)
        self.assertEqual(parse_event(rec), (EV_ABS, ABS_X, 1234))

    def test_short_record_rejected(self):
        with self.assertRaises(ValueError):
            parse_event(b"\x00" * 7)

    def test_signed_value_roundtrip(self):
        # ABS_MT_TRACKING_ID -1 (contact lifted) must survive packing:
        # the evdev value field is __s32, not unsigned.
        rec = pack_event(EV_ABS, ABS_MT_TRACKING_ID, -1)
        self.assertEqual(parse_event(rec),
                         (EV_ABS, ABS_MT_TRACKING_ID, -1))


class EventRecordSizeTest(unittest.TestCase):
    """Regression: 64-bit Linux struct input_event is 24 bytes.

    The reader once used "<llHHi" (16 bytes); on the 64-bit host the
    kernel emits 24-byte records (timeval with 8-byte longs) and
    rejects a 16-byte read() with EINVAL. These tests pin the 24-byte
    layout, the full-record read size, fragmented-read parsing, and
    signed values at the new size."""

    def test_supported_event_size_is_24_bytes(self):
        self.assertEqual(touch.EVENT_FORMAT, "<qqHHi")
        self.assertEqual(touch.EVENT_SIZE, 24)
        self.assertEqual(struct.calcsize(touch.EVENT_FORMAT), 24)

    def test_legacy_16_byte_record_rejected(self):
        # A 32-bit/old-format 16-byte record must fail loudly, never
        # misparse as a 64-bit event.
        legacy = struct.pack("<llHHi", 0, 0, EV_ABS, ABS_X, 1)
        self.assertEqual(len(legacy), 16)
        with self.assertRaises(ValueError):
            parse_event(legacy)

    def test_reads_request_full_record_size(self):
        # The kernel checks read() counts against its native 24-byte
        # record: every request must be exactly the remainder of one
        # full record, starting with a full EVENT_SIZE read.
        requested = []

        class RecordingStream(io.BytesIO):
            def read(self, n):
                requested.append(n)
                return super().read(n)

        cfg = default_config()
        svc = TouchService(cfg)
        blob = (pack_event(EV_ABS, ABS_X, 1000)
                + pack_event(EV_SYN, SYN_REPORT, 0))
        triples = list(svc.iter_device_events(RecordingStream(blob)))
        self.assertEqual(triples, [(EV_ABS, ABS_X, 1000),
                                   (EV_SYN, SYN_REPORT, 0)])
        self.assertTrue(requested)
        self.assertEqual(requested[0], 24)
        for size in requested:
            self.assertGreaterEqual(size, 1)
            self.assertLessEqual(size, 24)

    def test_fragmented_reads_still_parse(self):
        # byte-at-a-time (and other short) device reads must reassemble
        # into the same triples as a whole-record read.
        expected = [(EV_ABS, ABS_MT_TRACKING_ID, -1),
                    (EV_ABS, ABS_MT_POSITION_X, 1500),
                    (EV_SYN, SYN_REPORT, 0)]
        blob = b"".join(pack_event(*t) for t in expected)
        for chunk_size in (1, 7, 23):
            stream = _ChunkedStream(blob, chunk_size)
            svc = TouchService(default_config())
            self.assertEqual(list(svc.iter_device_events(stream)),
                             expected,
                             "chunk_size=%d" % chunk_size)

    def test_signed_tracking_id_at_24_bytes(self):
        # ABS_MT_TRACKING_ID -1 (contact lifted) stays signed in the
        # 24-byte layout; the value field is __s32, not unsigned.
        rec = pack_event(EV_ABS, ABS_MT_TRACKING_ID, -1)
        self.assertEqual(len(rec), 24)
        self.assertEqual(parse_event(rec),
                         (EV_ABS, ABS_MT_TRACKING_ID, -1))


class _ChunkedStream(io.RawIOBase):
    """Binary stream yielding at most chunk_size bytes per read()."""

    def __init__(self, blob, chunk_size):
        self._buf = io.BytesIO(blob)
        self._chunk = chunk_size

    def readable(self):
        return True

    def read(self, n=-1):
        size = self._chunk if n < 0 else min(n, self._chunk)
        return self._buf.read(size)


    def test_down_move_up(self):
        p = EvdevParser()
        events = p.feed_frame(st_down(1000, 2000))
        self.assertEqual([(e.kind, e.x, e.y) for e in events],
                         [("down", 1000, 2000)])
        events = p.feed_frame([(EV_ABS, ABS_X, 1100),
                               (EV_ABS, ABS_Y, 2100)])
        self.assertEqual([(e.kind, e.x, e.y) for e in events],
                         [("move", 1100, 2100)])
        events = p.feed_frame(st_up())
        self.assertEqual([e.kind for e in events], ["up"])

    def test_move_without_touch_is_silent(self):
        p = EvdevParser()
        self.assertEqual(p.feed_frame([(EV_ABS, ABS_X, 5)]), [])


class MultiTouchTest(unittest.TestCase):
    def test_two_slot_lifecycle(self):
        p = EvdevParser()
        events = p.feed_frame(mt_down(0, 10, 100, 200))
        self.assertEqual([(e.kind, e.slot) for e in events],
                         [("down", 0)])
        events = p.feed_frame(mt_down(1, 11, 300, 400))
        self.assertEqual([(e.kind, e.slot) for e in events],
                         [("down", 1)])
        events = p.feed_frame([(EV_ABS, ABS_MT_SLOT, 0),
                               (EV_ABS, ABS_MT_POSITION_X, 150)])
        self.assertEqual([(e.kind, e.slot, e.x) for e in events],
                         [("move", 0, 150)])
        # Slot 1 untouched by slot-0 move: no cross-talk.
        events = p.feed_frame(mt_up(0))
        self.assertEqual([(e.kind, e.slot) for e in events], [("up", 0)])
        events = p.feed_frame(mt_up(1))
        self.assertEqual([(e.kind, e.slot) for e in events], [("up", 1)])

    def test_mt_preferred_over_single_touch_echo(self):
        # Hybrid panels report both; one frame must not double-report.
        p = EvdevParser()
        events = p.feed_frame(mt_down(0, 7, 500, 600)
                              + [(EV_ABS, ABS_X, 500),
                                 (EV_ABS, ABS_Y, 600),
                                 (EV_KEY, BTN_TOUCH, 1)])
        downs = [e for e in events if e.kind == "down"]
        self.assertEqual(len(downs), 1)


class NormalizeTest(unittest.TestCase):
    def test_corners_and_center(self):
        self.assertEqual(normalize(0, 0, 1920, 1080, CAL), (0, 0))
        self.assertEqual(normalize(4095, 4095, 1920, 1080, CAL),
                         (1919, 1079))
        x, y = normalize(2047, 2047, 1920, 1080, CAL)
        self.assertTrue(900 <= x <= 1010 and 500 <= y <= 580)

    def test_clamps_out_of_range(self):
        self.assertEqual(normalize(-50, 99999, 1920, 1080, CAL),
                         (0, 1079))

    def test_invert_and_swap(self):
        cal = dict(CAL, invert_x=True)
        x_plain, _ = normalize(0, 0, 1920, 1080, CAL)
        x_inv, _ = normalize(0, 0, 1920, 1080, cal)
        self.assertEqual((x_plain, x_inv), (0, 1919))
        cal = dict(CAL, swap_xy=True)
        # A square display keeps numbers comparable after swap.
        self.assertEqual(normalize(4095, 0, 500, 500, cal), (0, 499))

    def test_rotation_90_cw(self):
        cal = dict(CAL, rotation=90)
        # Raw top-left maps to display top-right under 90cw.
        self.assertEqual(normalize(0, 0, 1920, 1080, cal), (1919, 0))

    def test_rotation_180(self):
        cal = dict(CAL, rotation=180)
        self.assertEqual(normalize(0, 0, 1920, 1080, cal), (1919, 1079))

    def test_bad_rotation_rejected(self):
        with self.assertRaises(ValueError):
            normalize(1, 1, 8, 8, dict(CAL, rotation=45))

    def test_missing_range_rejected(self):
        with self.assertRaises(ValueError):
            normalize(1, 1, 8, 8, {})

    def test_none_position_passthrough(self):
        self.assertEqual(normalize(None, 100, 1920, 1080, CAL),
                         (None, 26))


class HitTestTest(unittest.TestCase):
    REGIONS = [
        {"id": "next", "rect": [1280, 0, 640, 1080]},
        {"id": "wake", "rect": [0, 0, 640, 1080]},
    ]

    def test_hit_and_miss(self):
        self.assertEqual(hit_test(1500, 500, self.REGIONS), "next")
        self.assertEqual(hit_test(100, 500, self.REGIONS), "wake")
        self.assertIsNone(hit_test(800, 500, self.REGIONS))  # dead middle

    def test_first_region_wins_overlap(self):
        regions = [{"id": "a", "rect": [0, 0, 100, 100]},
                   {"id": "b", "rect": [0, 0, 100, 100]}]
        self.assertEqual(hit_test(10, 10, regions), "a")

    def test_none_position_misses(self):
        self.assertIsNone(hit_test(None, 5, self.REGIONS))


class ActionTest(unittest.TestCase):
    def test_allowlisted_actions(self):
        self.assertEqual(action_request({"name": "playlist_next"}),
                         ("POST", "/playlist/next", {}))
        self.assertEqual(action_request({"name": "screen_on"}),
                         ("POST", "/screen/on", {}))
        method, path, body = action_request(
            {"name": "show", "renderer": "clock", "params": {"lines": 3}})
        self.assertEqual((method, path), ("POST", "/show"))
        self.assertEqual(body["renderer"], "clock")

    def test_unknown_action_refused(self):
        with self.assertRaises(ValueError):
            action_request({"name": "exec"})
        with self.assertRaises(ValueError):
            action_request({"name": "POST /screen/off"})
        with self.assertRaises(ValueError):
            action_request({})

    def test_show_needs_renderer(self):
        with self.assertRaises(ValueError):
            action_request({"name": "show"})

    def test_dispatch_posts(self):
        calls = []

        class FakeHTTP:
            def post(self, path, body):
                calls.append((path, body))
                return 200, {"ok": True}

        client = DisplaydClient("http://127.0.0.1:9")
        client.post = FakeHTTP().post
        summary = client.dispatch({"name": "playlist_next"})
        self.assertEqual(calls, [("/playlist/next", {})])
        self.assertEqual(summary["status"], 200)

    def test_dispatch_dry_run_sends_nothing(self):
        client = DisplaydClient("http://127.0.0.1:9")
        client.post = lambda *a: (_ for _ in ()).throw(
            AssertionError("must not POST in dry-run"))
        summary = client.dispatch({"name": "screen_on"}, dry_run=True)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["path"], "/screen/on")


class TapDetectorTest(unittest.TestCase):
    def test_tap_emits_on_up(self):
        det = TapDetector()
        self.assertIsNone(det.feed(TouchEvent("down", 0, 100, 100), 0.0))
        tap = det.feed(TouchEvent("up", 0, 102, 101), 0.2)
        self.assertEqual(tap, (100, 100))

    def test_swipe_cancelled(self):
        det = TapDetector(tap_max_pixels=40)
        det.feed(TouchEvent("down", 0, 100, 100), 0.0)
        det.feed(TouchEvent("move", 0, 300, 100), 0.1)
        self.assertIsNone(det.feed(TouchEvent("up", 0, 300, 100), 0.2))

    def test_long_press_ignored(self):
        det = TapDetector(tap_max_seconds=0.5)
        det.feed(TouchEvent("down", 0, 100, 100), 0.0)
        self.assertIsNone(det.feed(TouchEvent("up", 0, 100, 100), 5.0))

    def test_debounce_second_finger(self):
        det = TapDetector(debounce_seconds=0.3)
        det.feed(TouchEvent("down", 0, 100, 100), 0.0)
        det.feed(TouchEvent("up", 0, 100, 100), 0.1)
        det.feed(TouchEvent("down", 1, 200, 200), 0.15)
        self.assertIsNone(det.feed(TouchEvent("up", 1, 200, 200), 0.2))


class ServiceHandleFrameTest(unittest.TestCase):
    def _service(self, **overrides):
        cfg = default_config()
        cfg.update({"width": 1920, "height": 1080,
                    "calibration": dict(CAL),
                    "tap_max_seconds": 60,
                    "debounce_seconds": 0})
        cfg.update(overrides)
        dispatched = []

        class FakeClient:
            def dispatch(self, action, dry_run=False):
                dispatched.append((action, dry_run))
                return {"action": action["name"], "dry_run": dry_run}

        svc = TouchService(cfg, client=FakeClient())
        return svc, dispatched

    def test_tap_in_region_dispatches(self):
        svc, dispatched = self._service()
        # Raw (4095, 2047) -> display right edge -> playlist-next region.
        svc.handle_frame([TouchEvent("down", 0, 4095, 2047)], dry_run=True)
        svc.handle_frame([TouchEvent("up", 0, 4095, 2047)], dry_run=True)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0][0]["name"], "playlist_next")

    def test_tap_in_dead_zone_dispatches_nothing(self):
        svc, dispatched = self._service()
        svc.handle_frame([TouchEvent("down", 0, 2047, 2047)], dry_run=True)
        svc.handle_frame([TouchEvent("up", 0, 2047, 2047)], dry_run=True)
        self.assertEqual(dispatched, [])

    def test_http_failure_does_not_raise(self):
        cfg = default_config()
        cfg.update({"width": 1920, "height": 1080,
                    "calibration": dict(CAL), "tap_max_seconds": 60,
                    "debounce_seconds": 0})

        class Boom:
            def dispatch(self, action, dry_run=False):
                raise RuntimeError("connection refused")

        svc = TouchService(cfg, client=Boom())
        svc.handle_frame([TouchEvent("down", 0, 4095, 2047)], dry_run=False)
        result = svc.handle_frame([TouchEvent("up", 0, 4095, 2047)],
                                  dry_run=False)
        self.assertIn("error", result)

    def test_device_stream_parses_records(self):
        svc, _ = self._service()
        blob = (pack_event(EV_ABS, ABS_X, 1000)
                + pack_event(EV_KEY, BTN_TOUCH, 1)
                + pack_event(EV_SYN, SYN_REPORT, 0))
        triples = list(svc.iter_device_events(io.BytesIO(blob)))
        self.assertEqual(triples, [(EV_ABS, ABS_X, 1000),
                                   (EV_KEY, BTN_TOUCH, 1),
                                   (EV_SYN, SYN_REPORT, 0)])


class TapDismissServiceTest(unittest.TestCase):
    """Reload tap-dismissal: every valid tap dismisses first.

    touch.py sends POST /touch/tap before hit-testing on every valid
    short stationary tap, so even center/dead-zone taps return an active
    reload view while region actions are unchanged. Rejected gestures
    (swipes, long presses, debounced echoes, incomplete lifecycles)
    dismiss nothing, and a failed dismissal never blocks the region
    action that follows."""

    CAL = {"x_min": 0, "x_max": 4095, "y_min": 0, "y_max": 4095}

    def _service(self, **overrides):
        cfg = default_config()
        cfg.update({"width": 1920, "height": 1080,
                    "calibration": dict(self.CAL),
                    "tap_max_seconds": 60,
                    "debounce_seconds": 0})
        cfg.update(overrides)
        calls = []

        class FakeClient:
            fail_dismiss = False

            def tap_dismiss(self, dry_run=False):
                calls.append(("dismiss", dry_run))
                if self.fail_dismiss:
                    raise RuntimeError("connection refused")
                return {"action": "tap_dismiss", "dry_run": dry_run}

            def dispatch(self, action, dry_run=False):
                calls.append(("dispatch", action["name"], dry_run))
                return {"action": action["name"], "dry_run": dry_run}

        client = FakeClient()
        svc = TouchService(cfg, client=client)
        return svc, calls, client

    def _tap(self, svc, raw_x, raw_y, dry_run=True):
        svc.handle_frame([TouchEvent("down", 0, raw_x, raw_y)],
                         dry_run=dry_run)
        return svc.handle_frame([TouchEvent("up", 0, raw_x, raw_y)],
                                dry_run=dry_run)

    def test_matched_tap_dismisses_first_then_dispatches(self):
        svc, calls, _ = self._service()
        # Raw (4095, 2047) -> display right edge -> playlist-next region.
        result = self._tap(svc, 4095, 2047)
        self.assertEqual(calls, [("dismiss", True),
                                 ("dispatch", "playlist_next", True)])
        self.assertEqual(result["action"], "playlist_next")

    def test_unmatched_center_tap_dismisses_without_dispatch(self):
        svc, calls, _ = self._service()
        # Raw (2047, 2047) -> display middle: the dead zone by design --
        # no region action, but the reload dismissal still goes out.
        result = self._tap(svc, 2047, 2047)
        self.assertEqual(calls, [("dismiss", True)])
        self.assertIsNone(result)

    def test_left_region_tap_preserves_action(self):
        svc, calls, _ = self._service()
        result = self._tap(svc, 0, 2047)
        self.assertEqual(calls, [("dismiss", True),
                                 ("dispatch", "screen_on", True)])
        self.assertEqual(result["action"], "screen_on")

    def test_down_without_up_dismisses_nothing(self):
        svc, calls, _ = self._service()
        svc.handle_frame([TouchEvent("down", 0, 4095, 2047)], dry_run=True)
        self.assertEqual(calls, [])

    def test_up_without_down_dismisses_nothing(self):
        svc, calls, _ = self._service()
        svc.handle_frame([TouchEvent("up", 0, 4095, 2047)], dry_run=True)
        self.assertEqual(calls, [])

    def test_swipe_dismisses_nothing(self):
        svc, calls, _ = self._service()
        svc.handle_frame([TouchEvent("down", 0, 500, 2047)], dry_run=True)
        # A far move cancels the candidate: it was a swipe, not a tap.
        svc.handle_frame([TouchEvent("move", 0, 2500, 2047)], dry_run=True)
        svc.handle_frame([TouchEvent("up", 0, 2500, 2047)], dry_run=True)
        self.assertEqual(calls, [])

    def test_long_press_dismisses_nothing(self):
        import unittest.mock as mock
        svc, calls, _ = self._service(tap_max_seconds=0.5)
        ticks = iter([100.0, 200.0])  # down now, up far later
        with mock.patch.object(touch.time, "monotonic",
                               side_effect=lambda: next(ticks)):
            svc.handle_frame([TouchEvent("down", 0, 4095, 2047)],
                             dry_run=True)
            svc.handle_frame([TouchEvent("up", 0, 4095, 2047)],
                             dry_run=True)
        self.assertEqual(calls, [])

    def test_debounced_second_tap_dismisses_nothing(self):
        svc, calls, _ = self._service(debounce_seconds=60)
        self._tap(svc, 4095, 2047)
        self.assertEqual(len(calls), 2)  # dismiss + dispatch
        # The immediate echo (multitouch bounce) is debounced: neither
        # dismissal nor dispatch goes out again.
        self._tap(svc, 4095, 2047)
        self.assertEqual(len(calls), 2)

    def test_dismiss_failure_still_dispatches_region_action(self):
        svc, calls, client = self._service()
        client.fail_dismiss = True
        result = self._tap(svc, 4095, 2047, dry_run=False)
        kinds = [c[0] for c in calls]
        self.assertEqual(kinds, ["dismiss", "dispatch"])
        self.assertEqual(calls[1][1], "playlist_next")
        self.assertEqual(result["action"], "playlist_next")

    def test_client_tap_dismiss_posts_touch_tap(self):
        posted = []

        class FakeHTTP:
            def post(self, path, body):
                posted.append((path, body))
                return 200, {"dismissed": True}

        client = DisplaydClient("http://127.0.0.1:9")
        client.post = FakeHTTP().post
        summary = client.tap_dismiss()
        self.assertEqual(posted, [("/touch/tap", {})])
        self.assertEqual(summary["status"], 200)
        dry = client.tap_dismiss(dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(len(posted), 1)  # dry run sends nothing


class ConfigTest(unittest.TestCase):
    def test_defaults_validate(self):
        cfg = load_config(None)
        self.assertEqual(cfg["device"], "/dev/input/event8")
        self.assertTrue(len(cfg["regions"]) >= 1)

    def test_file_overlay_and_bad_action_rejected(self):
        good = {"device": "/dev/input/event3", "width": 800, "height": 480,
                "calibration": {"x_max": 1023, "y_max": 1023},
                "regions": [{"id": "n", "rect": [0, 0, 800, 480],
                             "action": {"name": "playlist_next"}}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump(good, fh)
            path = fh.name
        try:
            cfg = load_config(path)
            self.assertEqual((cfg["device"], cfg["width"]), (
                "/dev/input/event3", 800))
        finally:
            os.unlink(path)
        bad = dict(good, regions=[{"id": "x", "rect": [0, 0, 1, 1],
                                         "action": {"name": "reboot"}}])
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump(bad, fh)
            path = fh.name
        try:
            with self.assertRaises(ValueError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_env_override(self):
        os.environ["DISPLAYD_TOUCH_DEVICE"] = "/dev/input/event9"
        try:
            self.assertEqual(load_config(None)["device"],
                             "/dev/input/event9")
        finally:
            del os.environ["DISPLAYD_TOUCH_DEVICE"]


class ConfidenceConfigTest(unittest.TestCase):
    def test_disabled_by_default(self):
        cfg = load_config(None)
        self.assertEqual(cfg["confidence_feedback"],
                         {"enabled": False,
                          "renderer": "touch_confidence", "input": "tap"})

    def test_normalize_accepts_bool_shorthand(self):
        enabled = normalize_confidence_feedback(True)
        self.assertTrue(enabled["enabled"])
        self.assertEqual(enabled["renderer"], "touch_confidence")
        disabled = normalize_confidence_feedback(False)
        self.assertFalse(disabled["enabled"])

    def test_normalize_rejects_garbage(self):
        with self.assertRaises(ValueError):
            normalize_confidence_feedback(42)
        with self.assertRaises(ValueError):
            normalize_confidence_feedback({"enabled": "yes"})
        with self.assertRaises(ValueError):
            normalize_confidence_feedback({"enabled": True,
                                           "renderer": ""})
        with self.assertRaises(ValueError):
            normalize_confidence_feedback({"enabled": True,
                                           "input": "a/b"})

    def test_file_overlay_bool_and_dict(self):
        for overlay, enabled in (({"confidence_feedback": True}, True),
                                 ({"confidence_feedback": False}, False),
                                 ({"confidence_feedback":
                                   {"enabled": True, "renderer": "tc",
                                    "input": "taps"}}, True)):
            with tempfile.NamedTemporaryFile("w", suffix=".json",
                                             delete=False) as fh:
                json.dump(overlay, fh)
                path = fh.name
            try:
                cfg = load_config(path)
            finally:
                os.unlink(path)
            self.assertEqual(cfg["confidence_feedback"]["enabled"],
                             enabled)
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump({"confidence_feedback": {"enabled": True,
                                                "renderer": "tc",
                                                "input": "taps"}}, fh)
            path = fh.name
        try:
            cfg = load_config(path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg["confidence_feedback"]["renderer"], "tc")
        self.assertEqual(cfg["confidence_feedback"]["input"], "taps")

    def test_file_overlay_bad_value_rejected(self):
        for bad in ({"confidence_feedback": 42},
                    {"confidence_feedback": {"enabled": "yes"}}):
            with tempfile.NamedTemporaryFile("w", suffix=".json",
                                             delete=False) as fh:
                json.dump(bad, fh)
                path = fh.name
            try:
                with self.assertRaises(ValueError):
                    load_config(path)
            finally:
                os.unlink(path)

    def test_env_override(self):
        for text, enabled in (("1", True), ("true", True), ("off", False),
                              ("0", False)):
            os.environ["DISPLAYD_TOUCH_CONFIDENCE"] = text
            try:
                cfg = load_config(None)
            finally:
                del os.environ["DISPLAYD_TOUCH_CONFIDENCE"]
            self.assertEqual(cfg["confidence_feedback"]["enabled"],
                             enabled, "env=%r" % text)
        os.environ["DISPLAYD_TOUCH_CONFIDENCE"] = "maybe"
        try:
            with self.assertRaises(ValueError):
                load_config(None)
        finally:
            del os.environ["DISPLAYD_TOUCH_CONFIDENCE"]

    def test_env_parser_units(self):
        self.assertEqual(confidence_env_override("ON"), {"enabled": True})
        self.assertEqual(confidence_env_override("no"), {"enabled": False})
        with self.assertRaises(ValueError):
            confidence_env_override("maybe")


class ConfidenceFeedbackTest(unittest.TestCase):
    RIGHT_RAW = (4095, 2047)   # display right edge -> playlist-next region
    LEFT_RAW = (0, 100)         # display left edge -> screen-on region
    DEAD_RAW = (2047, 2047)     # display middle -> dead zone

    def _service(self, enabled=True, **overrides):
        cfg = default_config()
        cfg.update({"width": 1920, "height": 1080,
                    "calibration": dict(CAL),
                    "tap_max_seconds": 60,
                    "debounce_seconds": 0,
                    "confidence_feedback": {"enabled": enabled}})
        cfg.update(overrides)
        events = []

        class RecordingClient:
            def tap_dismiss(self, dry_run=False):
                events.append(("dismiss", dry_run))
                return {"action": "tap_dismiss", "dry_run": dry_run}

            def dispatch(self, action, dry_run=False):
                events.append(("dispatch", action.get("name"), dry_run))
                return {"action": action.get("name"), "status": 200,
                        "dry_run": dry_run}

            def post(self, path, body):
                events.append(("feedback", path, dict(body)))
                return 200, {"ok": True}

        svc = TouchService(cfg, client=RecordingClient())
        return svc, events

    def _tap(self, svc, raw, dry_run=False):
        svc.handle_frame([TouchEvent("down", 0, raw[0], raw[1])],
                         dry_run=dry_run)
        return svc.handle_frame([TouchEvent("up", 0, raw[0], raw[1])],
                                dry_run=dry_run)

    def test_hit_reports_after_dispatch_in_order(self):
        svc, events = self._service(enabled=True)
        summary = self._tap(svc, self.RIGHT_RAW)
        # The configured action is preserved exactly...
        self.assertEqual(summary["action"], "playlist_next")
        self.assertEqual(summary["status"], 200)
        # ...and feedback follows the dispatch, never precedes it.
        # (Every valid tap also dismisses reload first, best-effort.)
        self.assertEqual([e[0] for e in events],
                         ["dismiss", "dispatch", "feedback"])
        kind, path, payload = events[2]
        self.assertEqual(path, "/feed/touch_confidence/tap")
        self.assertEqual(payload["region"], "playlist-next")
        self.assertEqual(payload["action"], "playlist_next")
        self.assertTrue(payload["hit"])
        self.assertEqual(payload["result"], "dispatched")
        self.assertNotIn("error", payload)

    def test_normalized_coordinates(self):
        svc, events = self._service(enabled=True)
        payload = svc.confidence_payload((0, 0), "screen-on", "screen_on")
        self.assertEqual((payload["x"], payload["y"]), (0, 0))
        self.assertEqual((payload["x_norm"], payload["y_norm"]),
                         (0.0, 0.0))
        payload = svc.confidence_payload((1919, 1079), "playlist-next",
                                         "playlist_next")
        self.assertEqual((payload["x"], payload["y"]), (1919, 1079))
        self.assertEqual((payload["x_norm"], payload["y_norm"]),
                         (1.0, 1.0))
        # End to end: raw corners map to pixel corners in the posted feed.
        self._tap(svc, (0, 0))
        posted = events[-1][2]
        self.assertEqual((posted["x"], posted["y"]), (0, 0))
        self.assertEqual((posted["x_norm"], posted["y_norm"]),
                         (0.0, 0.0))

    def test_dead_zone_feedback_dispatches_no_action(self):
        svc, events = self._service(enabled=True)
        summary = self._tap(svc, self.DEAD_RAW)
        self.assertIsNone(summary)  # no ordinary action
        # Dead-zone tap: reload dismissal still goes out, then feedback.
        self.assertEqual([e[0] for e in events],
                         ["dismiss", "feedback"])
        kind, path, payload = events[1]
        self.assertEqual(path, "/feed/touch_confidence/tap")
        self.assertFalse(payload["hit"])
        self.assertNotIn("region", payload)
        self.assertNotIn("action", payload)
        self.assertEqual(payload["result"], "dead-zone")

    def test_custom_feed_target(self):
        svc, events = self._service(
            enabled=True,
            confidence_feedback={"enabled": True, "renderer": "tc2",
                                 "input": "taps"})
        self._tap(svc, self.LEFT_RAW)
        self.assertEqual(events[2][1], "/feed/tc2/taps")

    def test_best_effort_feedback_failure_keeps_action(self):
        cfg = default_config()
        cfg.update({"width": 1920, "height": 1080,
                    "calibration": dict(CAL), "tap_max_seconds": 60,
                    "debounce_seconds": 0,
                    "confidence_feedback": {"enabled": True}})

        class FlakyClient:
            def __init__(self):
                self.dispatched = []

            def dispatch(self, action, dry_run=False):
                self.dispatched.append(action.get("name"))
                return {"action": action.get("name"), "status": 200}

            def post(self, path, body):
                raise RuntimeError("displayd restarting")

        client = FlakyClient()
        svc = TouchService(cfg, client=client)
        summary = self._tap(svc, self.RIGHT_RAW)
        self.assertEqual(summary["action"], "playlist_next")
        self.assertEqual(client.dispatched, ["playlist_next"])
        # Dead-zone tap with a failing feed: still silent, still no raise.
        self.assertIsNone(self._tap(svc, self.DEAD_RAW))

    def test_dispatch_error_is_reported_not_hidden(self):
        cfg = default_config()
        cfg.update({"width": 1920, "height": 1080,
                    "calibration": dict(CAL), "tap_max_seconds": 60,
                    "debounce_seconds": 0,
                    "confidence_feedback": {"enabled": True}})
        posted = []

        class BoomDispatch:
            def tap_dismiss(self, dry_run=False):
                return {"action": "tap_dismiss", "dry_run": dry_run}

            def dispatch(self, action, dry_run=False):
                raise RuntimeError("connection refused")

            def post(self, path, body):
                posted.append((path, dict(body)))
                return 200, {}

        svc = TouchService(cfg, client=BoomDispatch())
        summary = self._tap(svc, self.RIGHT_RAW)
        self.assertIn("error", summary)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][1]["result"], "dispatch-error")
        self.assertIn("refused", posted[0][1]["error"])

    def test_disabled_mode_sends_nothing(self):
        svc, events = self._service(enabled=False)

        def boom_post(path, body):
            raise AssertionError("feedback must not POST when disabled")
        svc.client.post = boom_post
        summary = self._tap(svc, self.RIGHT_RAW)
        self.assertEqual(summary["action"], "playlist_next")
        self.assertEqual([e[0] for e in events],
                         ["dismiss", "dispatch"])
        self.assertIsNone(self._tap(svc, self.DEAD_RAW))
        self.assertEqual([e[0] for e in events],
                         ["dismiss", "dispatch", "dismiss"])

    def test_absent_switch_behaves_as_disabled(self):
        cfg = default_config()
        del cfg["confidence_feedback"]
        cfg.update({"width": 1920, "height": 1080,
                    "calibration": dict(CAL), "tap_max_seconds": 60,
                    "debounce_seconds": 0})
        dispatched = []

        class NoPostClient:
            def dispatch(self, action, dry_run=False):
                dispatched.append(action.get("name"))
                return {"action": action.get("name")}

        svc = TouchService(cfg, client=NoPostClient())
        # No `post` method at all: any feedback attempt would AttributeError.
        self.assertEqual(
            self._tap(svc, self.RIGHT_RAW)["action"], "playlist_next")
        self.assertIsNone(self._tap(svc, self.DEAD_RAW))
        self.assertEqual(dispatched, ["playlist_next"])

    def test_dry_run_sends_nothing(self):
        svc, events = self._service(enabled=True)
        summary = self._tap(svc, self.RIGHT_RAW, dry_run=True)
        self.assertTrue(summary["dry_run"])
        self.assertEqual([e[0] for e in events],
                         ["dismiss", "dispatch"])
        self.assertTrue(events[1][2])  # dispatch itself was dry-run
        self.assertIsNone(self._tap(svc, self.DEAD_RAW, dry_run=True))
        self.assertEqual([e[0] for e in events],
                         ["dismiss", "dispatch", "dismiss"])

    def test_swipe_emits_nothing(self):
        svc, events = self._service(enabled=True)
        # Raw 100 -> ~47px, raw 1000 -> ~469px: far past the 40px budget.
        svc.handle_frame([TouchEvent("down", 0, 100, 2047)])
        svc.handle_frame([TouchEvent("move", 0, 1000, 2047)])
        svc.handle_frame([TouchEvent("up", 0, 1000, 2047)])
        self.assertEqual(events, [])

    def test_long_press_emits_nothing(self):
        svc, events = self._service(enabled=True)
        svc.handle_frame([TouchEvent("down", 0, 4095, 2047)])
        # Age the pending down past tap_max_seconds, then release.
        slot = 0
        x, y, _t0 = svc.taps._pending[slot]
        svc.taps._pending[slot] = (x, y, time.monotonic() - 3600)
        svc.handle_frame([TouchEvent("up", 0, 4095, 2047)])
        self.assertEqual(events, [])

    def test_incomplete_event_emits_nothing(self):
        svc, events = self._service(enabled=True)
        # Contact without a position yet: no candidate, no feedback.
        svc.handle_frame([TouchEvent("down", 0, None, 2047)])
        svc.handle_frame([TouchEvent("up", 0, None, 2047)])
        self.assertEqual(events, [])

    def test_debounce_suppressed_tap_emits_nothing(self):
        svc, events = self._service(
            enabled=True, tap_max_seconds=60, debounce_seconds=0.3)
        self._tap(svc, self.RIGHT_RAW)
        self.assertEqual([e[0] for e in events],
                         ["dismiss", "dispatch", "feedback"])
        # Immediate second tap: the detector eats it as bounce echo.
        svc.handle_frame([TouchEvent("down", 1, 4095, 2047)])
        svc.handle_frame([TouchEvent("up", 1, 4095, 2047)])
        self.assertEqual([e[0] for e in events],
                         ["dismiss", "dispatch", "feedback"])


class GuardShapeTest(unittest.TestCase):
    """The parlay-guard-shaped allowlist: closed table + silent denies.

    Mirrors trillium/parlay packages/server/src/guard/ (paths.ts: closed
    set classified by handler effect; index.ts: silent denies)."""

    def test_table_is_the_closed_set(self):
        self.assertEqual(set(ACTION_TABLE), {
            "playlist_next", "playlist_pause", "playlist_resume",
            "screen_on", "screen_off", "clear", "show", "notify",
            "feedback",
        })
        # Every entry classifies by handler effect: a daemon endpoint the
        # tap drives, never just a name.
        for name, spec in ACTION_TABLE.items():
            self.assertTrue(spec.get("effect"), name)
            self.assertEqual(spec["method"], "POST", name)
            self.assertTrue(spec["path"].startswith("/"), name)

    def test_named_residue_stays_out(self):
        # No generic call-any-URL action, no shell-out, no free-text
        # feedback passthrough: a bad config cannot become command
        # execution. If a future diff adds one of these names, this test
        # forces the decision to be deliberate.
        for forbidden in ("exec", "shell", "post", "get", "fetch",
                          "url", "command", "run"):
            self.assertNotIn(forbidden, ACTION_TABLE)
            self.assertIsNone(resolve_action({"name": forbidden}))

    def test_unknown_action_denied_silently(self):
        # Silent (index.ts shape): None, no exception -- the service loop
        # keeps serving touches.
        for bad in ({"name": "exec"}, {"name": "POST /screen/off"},
                    {}, {"name": None}, "playlist_next"):
            self.assertIsNone(resolve_action(bad))
        # Strict variant still raises for fail-fast config validation.
        with self.assertRaises(ValueError):
            action_request({"name": "exec"})

    def test_dispatch_denies_unknown_without_http(self):
        posted = []

        class RecordingClient(DisplaydClient):
            def post(self, path, body):
                posted.append((path, body))
                return 200, {"ok": True}

        client = RecordingClient("http://127.0.0.1:9")
        summary = client.dispatch({"name": "exec"})
        self.assertIn("error", summary)
        self.assertEqual(summary["action"], "exec")
        self.assertEqual(posted, [])  # not a byte on the wire


class CallerRuleTest(unittest.TestCase):
    """endpoint_allowed(): loopback or tailnet only (origin.ts shape)."""

    def test_loopback_allowed(self):
        for url in ("http://127.0.0.1:8980", "http://127.9.9.9/",
                    "http://localhost:8980",
                    "http://LOCALHOST:8980",
                    "http://[::1]:8980",
                    "https://127.0.0.1:8980"):
            self.assertTrue(endpoint_allowed(url), url)

    def test_tailnet_allowed(self):
        for url in ("http://100.81.88.113:8980",  # lnx-server
                    "http://100.64.0.1:8980",
                    "http://100.127.255.254:8980"):
            self.assertTrue(endpoint_allowed(url), url)

    def test_everything_else_denied(self):
        for url in ("http://192.168.1.5:8980",   # LAN is NOT enough
                    "http://10.0.0.2:8980",
                    "http://172.16.0.2:8980",
                    "http://8.8.8.8:8980",        # open internet
                    "http://100.63.255.255:8980",  # just below CGNAT
                    "http://100.128.0.0:8980",     # just above CGNAT
                    "http://lnx-server:8980",      # MagicDNS: use the IP
                    "http://displayd.local:8980",
                    "https://example.com/",
                    "file:///etc/passwd",
                    "", "not-a-url", "http://"):
            self.assertFalse(endpoint_allowed(url), url)

    def test_bad_endpoint_fails_config_before_device_open(self):
        overlay = {"endpoint": "http://8.8.8.8:8980"}
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump(overlay, fh)
            path = fh.name
        try:
            with self.assertRaises(ValueError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_dispatch_refuses_disallowed_endpoint_without_http(self):
        posted = []

        class RecordingClient(DisplaydClient):
            def post(self, path, body):
                posted.append((path, body))
                return 200, {"ok": True}

        # Constructed directly (bypassing load_config, as --endpoint
        # once allowed): dispatch still refuses, silently.
        client = RecordingClient("http://8.8.8.8:8980")
        summary = client.dispatch({"name": "screen_on"})
        self.assertIn("error", summary)
        self.assertEqual(posted, [])


class FeedbackActionTest(unittest.TestCase):
    """The first named action under the new table: tap-to-rate."""

    def test_feedback_resolves_to_fixed_shape(self):
        method, path, body = action_request(
            {"name": "feedback", "view": "clock", "rating": 5})
        self.assertEqual((method, path), ("POST", "/feedback"))
        self.assertEqual(body, {"view": "clock", "rating": 5,
                                "agent": "touch"})

    def test_feedback_categories_optional_but_closed(self):
        _, _, body = action_request(
            {"name": "feedback", "view": "clock", "rating": 4,
             "categories": ["legible"]})
        self.assertEqual(body["categories"], ["legible"])
        # notes/params are NOT passed through (named residue): the body
        # stays a fixed-shape rating.
        _, _, body = action_request(
            {"name": "feedback", "view": "clock", "rating": 4,
             "notes": "hello", "params": {"x": 1}})
        self.assertNotIn("notes", body)
        self.assertNotIn("params", body)

    def test_feedback_validation(self):
        for bad in ({"name": "feedback"},
                    {"name": "feedback", "view": "", "rating": 5},
                    {"name": "feedback", "view": "clock"},
                    {"name": "feedback", "view": "clock",
                     "rating": 0},
                    {"name": "feedback", "view": "clock",
                     "rating": 6},
                    {"name": "feedback", "view": "clock",
                     "rating": True},
                    {"name": "feedback", "view": "clock",
                     "rating": "5"},
                    {"name": "feedback", "view": "clock",
                     "rating": 5, "categories": "legible"},
                    {"name": "feedback", "view": "clock",
                     "rating": 5, "categories": [""]}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                action_request(bad)
            self.assertIsNone(resolve_action(bad))

    def test_feedback_dispatches(self):
        calls = []

        class FakeHTTP:
            def post(self, path, body):
                calls.append((path, body))
                return 201, {"ok": True}

        client = DisplaydClient("http://127.0.0.1:9")
        client.post = FakeHTTP().post
        summary = client.dispatch({"name": "feedback", "view": "clock",
                                   "rating": 5})
        self.assertEqual(calls, [("/feedback", {"view": "clock",
                                                  "rating": 5,
                                                  "agent": "touch"})])
        self.assertEqual(summary["status"], 201)

    def test_feedback_in_region_end_to_end(self):
        cfg = default_config()
        cfg.update({
            "width": 1920, "height": 1080,
            "calibration": dict(CAL),
            "tap_max_seconds": 60, "debounce_seconds": 0,
            "endpoint": "http://127.0.0.1:8980",
            "regions": [
                {"id": "rate-it", "rect": [640, 840, 640, 240],
                 "action": {"name": "feedback", "view": "clock",
                             "rating": 5}},
            ],
        })
        posted = []

        class FakeClient:
            def dispatch(self, action, dry_run=False):
                method, path, body = action_request(action)
                posted.append((path, body))
                return {"action": action["name"], "method": method,
                        "path": path, "body": body, "status": 201}

        svc = TouchService(cfg, client=FakeClient())
        # Raw (2047, 3900) -> display ~(959, 1029): inside the rate strip.
        svc.handle_frame([TouchEvent("down", 0, 2047, 3900)],
                         dry_run=True)
        summary = svc.handle_frame([TouchEvent("up", 0, 2047, 3900)],
                                   dry_run=True)
        self.assertEqual(summary["path"], "/feedback")
        self.assertEqual(posted, [("/feedback", {"view": "clock",
                                                   "rating": 5,
                                                   "agent": "touch"})])


if __name__ == "__main__":
    unittest.main()
