"""Tests for the beads-detail renderer. No framebuffer needed."""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
from PIL import Image

from renderers import beads_common as common
from renderers import beads_detail as detail


class FakeScreen:
    W, H = 1920, 1080

    def __init__(self, inputs=None):
        self.frames = []
        self.inputs = inputs or []

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    def clear(self, background=(0, 0, 0)):
        self.present(self.new_image(background))

    def get_input(self, renderer, name):
        assert renderer == "beads-detail"
        return list(self.inputs)

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        return displayd.Screen.color(value, default)

    @staticmethod
    def font_path(family="DejaVuSans-Bold"):
        return displayd.Screen.font_path(family)


def raw(iid, status="open", deps=(), labels=None, priority=2, **kw):
    doc = {"id": iid, "title": "title " + iid, "status": status,
           "priority": priority, "labels": list(labels or []),
           "dependencies": [{"issue_id": iid, "depends_on_id": t, "type": k}
                            for k, t in deps]}
    doc.update(kw)
    return doc


NH3Y = raw("task-nh3y", "open", deps=[("blocks", "task-ac7w")], priority=1,
           title="rango: decide daemon IPC authentication before merging PR #1",
           description=" MVP daemon hardening scope: which of auth to require.",
           owner="trillium@macbookpro", created_at="2026-08-13T16:08:11Z",
           updated_at="2026-08-13T21:35:19Z", comment_count=1,
           comments=[{"author": "Trillium Smith", "text": "holding for scope call",
                      "created_at": "2026-08-13T21:00:00Z"}])
AC7W = raw("task-ac7w", "open", priority=2, title="Gate: human")


def snap_of(*docs):
    return common._classify([("task", d) for d in docs])


class TestTarget(unittest.TestCase):
    def setUp(self):
        detail._LAST_FOCUS["bead_id"] = None

    def tearDown(self):
        detail._LAST_FOCUS["bead_id"] = None

    def test_param_seeds(self):
        screen = FakeScreen()
        self.assertEqual(detail.current_target(screen, "task-nh3y"), "task-nh3y")

    def test_newest_input_wins_over_param(self):
        screen = FakeScreen(inputs=[{"bead_id": "task-aaaa"},
                                    {"bead_id": "task-bbbb"}])
        self.assertEqual(detail.current_target(screen, "task-nh3y"), "task-bbbb")

    def test_memory_survives_no_param_no_input(self):
        screen = FakeScreen()
        detail.current_target(screen, "task-nh3y")
        self.assertEqual(detail.current_target(FakeScreen(), None), "task-nh3y")

    def test_none_when_nothing_known(self):
        self.assertIsNone(detail.current_target(FakeScreen(), None))

    def test_old_core_without_get_input(self):
        class OldScreen(FakeScreen):
            def get_input(self, renderer, name):
                raise AttributeError("no feeds here")
        # AttributeError inside getter -> treated as no inputs, param wins.
        screen = OldScreen()
        screen.get_input = None  # no feed mechanism at all
        self.assertEqual(detail.current_target(screen, "task-nh3y"), "task-nh3y")


class TestCard(unittest.TestCase):
    def setUp(self):
        with common._POLL["lock"]:
            common._POLL.update(snapshot=None, updated=0.0, health="cold",
                                error=None, source=None, focus_cache={},
                                focus_fetching=set())
        detail._LAST_FOCUS["bead_id"] = None

    def tearDown(self):
        with common._POLL["lock"]:
            common._POLL.update(snapshot=None, updated=0.0, health="cold",
                                error=None, source=None, focus_cache={},
                                focus_fetching=set())
        detail._LAST_FOCUS["bead_id"] = None

    def test_card_renders_real_bead(self):
        snap = snap_of(NH3Y, AC7W)
        with common._POLL["lock"]:
            common._POLL.update(snapshot=snap, updated=time.time(),
                                health="warm", source="test")
        issue, _ = common.resolve_focus("task-nh3y", snap)
        self.assertIsNotNone(issue)
        bucket, waiters = common.bucket_of(issue, snap)
        self.assertEqual(bucket, "stalled")
        self.assertEqual([w["id"] for w in waiters], ["task-ac7w"])
        screen = FakeScreen()
        detail._draw_card(screen, issue, bucket, waiters,
                          common.dependents_of(issue, snap),
                          snap, "warm", time.time(), "test", (8, 8, 12))
        self.assertTrue(screen.frames)
        img = screen.frames[-1]
        self.assertEqual(img.size, (1920, 1080))
        self.assertTrue(any(img.tobytes()), "card must not be blank")

    def test_bucket_agrees_with_overview(self):
        snap = snap_of(NH3Y, AC7W,
                       raw("r1", "in_progress"), raw("c1", "closed"))
        by_id = {i["id"]: b for b in ("rolling", "linedup", "stalled", "past")
                 for i in snap[b]}
        for bid, bucket in by_id.items():
            issue = snap["bare"][bid]
            self.assertEqual(common.bucket_of(issue, snap)[0], bucket, bid)

    def test_unknown_bead_draws_not_found(self):
        snap = snap_of(AC7W)
        with common._POLL["lock"]:
            common._POLL.update(snapshot=snap, updated=time.time(),
                                health="warm", source="test")
        screen = FakeScreen()
        detail._draw_empty(screen, "no such bead: task-nope", "hint",
                           None, (8, 8, 12))
        self.assertTrue(screen.frames)
        self.assertTrue(any(screen.frames[-1].tobytes()))

    def test_cold_cache_never_claims_no_such_bead(self):
        # The wall lie that caused the confusion: snap None + focus set
        # must admit the poll has not returned, never slander the bead.
        msg, hint = detail.empty_state("task-nh3y", None, 0)
        self.assertIn("looking for task-nh3y", msg)
        self.assertNotIn("no such bead", msg)
        self.assertIn("waiting for first poll", hint)

    def test_empty_states(self):
        msg, _ = detail.empty_state(None, None, 0)
        self.assertIn("no bead selected", msg)
        snap = snap_of(AC7W)
        msg, _ = detail.empty_state(None, snap, time.time())
        self.assertIn("no bead selected", msg)
        msg, hint = detail.empty_state("task-nope", snap, time.time())
        self.assertIn("no such bead: task-nope", msg)
        self.assertIn("still looking", hint)

    def test_run_presents_without_data(self):
        screen = FakeScreen()
        stop = threading.Event()
        worker = threading.Thread(
            target=detail.run,
            args=(screen, {"focus": "task-nope", "stores": "", "interval": 5},
                  stop))
        worker.start()
        time.sleep(2.5)
        stop.set()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertTrue(screen.frames, "run must present at least one frame")

    def test_find_prefix(self):
        snap = snap_of(NH3Y, AC7W)
        self.assertEqual(common.find_in_snapshot(snap, "task-nh3")["id"],
                         "task-nh3y")
        self.assertIsNone(common.find_in_snapshot(snap, "task-"))


class TestContract(unittest.TestCase):
    def test_detail_loads_and_advertises(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("beads-detail", found)
        self.assertIn("module", found["beads-detail"],
                      found["beads-detail"].get("broken"))
        mod = found["beads-detail"]["module"]
        self.assertTrue(callable(mod.run))
        self.assertFalse(mod.STATIC)
        for pname, spec in mod.PARAMS.items():
            self.assertIn(spec.get("type"), ("string", "integer",
                                             "number", "boolean"))

    def test_focus_input_declared(self):
        mod = displayd.load_renderers(displayd.RENDERER_DIR)["beads-detail"]["module"]
        self.assertIn("focus", mod.INPUTS)
        spec = mod.INPUTS["focus"]
        self.assertEqual(spec.get("type"), "object")
        self.assertIn("bead_id", spec.get("required") or [])

    def test_helper_is_not_a_renderer(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertNotIn("beads_common", found)


if __name__ == "__main__":
    unittest.main()
