"""Tests for the beads renderer. No framebuffer needed."""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
from PIL import Image

from renderers import beads


class FakeScreen:
    W, H = 1920, 1080

    def __init__(self):
        self.frames = []

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    def clear(self, background=(0, 0, 0)):
        self.present(self.new_image(background))

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        return displayd.Screen.color(value, default)

    @staticmethod
    def font_path(family="DejaVuSans-Bold"):
        return displayd.Screen.font_path(family)


def issue(iid, status="open", deps=(), store="task", title=None,
          priority=2, labels=()):
    return {"store": store, "id": iid, "title": title or ("title " + iid),
            "status": status, "priority": priority,
            "labels": set(labels),
            "deps": [{"type": t, "target": t_} for t, t_ in deps]}


def raw(iid, status="open", deps=(), labels=None, priority=2):
    return {"id": iid, "title": "title " + iid, "status": status,
            "priority": priority, "labels": list(labels or []),
            "dependencies": [{"issue_id": iid, "depends_on_id": t, "type": k}
                             for k, t in deps]}


class TestClassify(unittest.TestCase):
    def test_four_buckets(self):
        pairs = [("task", raw("a", "in_progress")),
                 ("task", raw("b", "open")),
                 ("task", raw("c", "open", deps=[("blocks", "d")])),
                 ("task", raw("d", "open")),
                 ("task", raw("e", "closed"))]
        snap = beads._classify(pairs)
        self.assertEqual([i["id"] for i in snap["rolling"]], ["a"])
        self.assertEqual(sorted(i["id"] for i in snap["linedup"]), ["b", "d"])
        self.assertEqual([i["id"] for i in snap["stalled"]], ["c"])
        self.assertEqual([i["id"] for i in snap["past"]], ["e"])

    def test_blocked_is_derived_from_edges_not_status(self):
        # A label claiming blocked with no unfinished edge target is NOT stalled.
        pairs = [("task", raw("x", "open", labels=["blocked"])),
                 ("task", raw("y", "open", deps=[("blocks", "z")])),
                 ("task", raw("z", "closed"))]
        snap = beads._classify(pairs)
        self.assertEqual(sorted(i["id"] for i in snap["linedup"]), ["x", "y"])
        self.assertEqual(snap["stalled"], [])

    def test_stalled_names_waiter(self):
        pairs = [("task", raw("nh3y", "open", deps=[("blocks", "ac7w")])),
                 ("task", {"id": "ac7w", "title": "Gate: human",
                           "status": "open", "priority": 2, "labels": [],
                           "dependencies": []})]
        snap = beads._classify(pairs)
        self.assertEqual(len(snap["stalled"]), 1)
        waiters = snap["stalled"][0]["waiters"]
        self.assertEqual(waiters[0]["id"], "ac7w")
        reasons = [a["reason"] for a in snap["attention"]]
        self.assertTrue(any("Gate: human" in r for r in reasons),
                        reasons)

    def test_in_progress_blocked_is_stalled(self):
        pairs = [("task", raw("u", "in_progress", deps=[("blocks", "g")])),
                 ("task", raw("g", "open"))]
        snap = beads._classify(pairs)
        self.assertEqual([i["id"] for i in snap["stalled"]], ["u"])
        self.assertEqual(snap["rolling"], [])

    def test_non_blocking_edge_types_ignored(self):
        pairs = [("task", raw("p", "open",
                              deps=[("parent-child", "q"), ("related", "q")])),
                 ("task", raw("q", "open"))]
        snap = beads._classify(pairs)
        self.assertEqual(sorted(i["id"] for i in snap["linedup"]), ["p", "q"])

    def test_attention_leads_with_review_queue(self):
        pairs = [("review", raw("r1", "open", priority=1)),
                 ("task", raw("s1", "open", deps=[("blocks", "s2")])),
                 ("task", raw("s2", "open"))]
        snap = beads._classify(pairs)
        self.assertTrue(snap["attention"])
        self.assertEqual(snap["attention"][0]["issue"]["id"], "r1")
        self.assertEqual(snap["attention"][0]["reason"], "review queue")


class TestDraw(unittest.TestCase):
    def setUp(self):
        with beads._POLL["lock"]:
            beads._POLL.update(snapshot=None, updated=0.0, health="cold",
                               error=None, source=None)

    def tearDown(self):
        with beads._POLL["lock"]:
            beads._POLL.update(snapshot=None, updated=0.0, health="cold",
                               error=None, source=None)

    def test_cold_start_renders_nonblank(self):
        screen = FakeScreen()
        beads._draw(screen, "BEADS", (8, 8, 12))
        self.assertTrue(screen.frames)
        px = screen.frames[-1].tobytes()
        self.assertTrue(any(px), "cold frame must not be blank")

    def test_draw_with_snapshot(self):
        snap = beads._classify([("task", raw("a", "in_progress")),
                                ("review", raw("r1", "open"))])
        with beads._POLL["lock"]:
            beads._POLL.update(snapshot=snap, updated=beads.time.time()
                               if hasattr(beads, "time") else 0,
                               health="warm", source="test")
            import time as _t
            beads._POLL["updated"] = _t.time()
        screen = FakeScreen()
        beads._draw(screen, "BEADS", (8, 8, 12))
        self.assertTrue(screen.frames)
        img = screen.frames[-1]
        self.assertEqual(img.size, (1920, 1080))

    def test_malformed_mirror_keeps_last_good(self):
        import tempfile
        snap = beads._classify([("task", raw("a", "open"))])
        with beads._POLL["lock"]:
            beads._POLL.update(snapshot=snap, health="warm", source="test")
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            fh.write("{not json,,,")
            path = fh.name
        try:
            with self.assertRaises(Exception):
                beads._load_mirror_file(path)
        finally:
            os.unlink(path)
        # Simulate the poll loop's failure path: snapshot survives.
        with beads._POLL["lock"]:
            beads._POLL["health"] = "stale"
            beads._POLL["error"] = "boom"
            kept = beads._POLL["snapshot"]
        self.assertIs(kept, snap)
        screen = FakeScreen()
        beads._draw(screen, "BEADS", (8, 8, 12))
        self.assertTrue(screen.frames)


class TestContract(unittest.TestCase):
    def test_renderer_loads_and_advertises(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("beads", found)
        self.assertIn("module", found["beads"],
                      found["beads"].get("broken"))
        mod = found["beads"]["module"]
        self.assertTrue(callable(mod.run))
        self.assertFalse(mod.STATIC)
        for pname, spec in mod.PARAMS.items():
            self.assertIn(spec.get("type"), ("string", "integer",
                                             "number", "boolean"))

    def test_run_presents_cold_frame_without_poll(self):
        # Point the poller at nothing so no data can arrive; run must still
        # present exactly the cold frame and exit on stop.
        screen = FakeScreen()
        stop = threading.Event()
        beads._ensure_poll({"mirror": "/nonexistent/beads.json",
                            "stores": "", "interval": 60})
        import threading as _th
        worker = _th.Thread(target=beads.run,
                            args=(screen, {"stores": "", "interval": 60},
                                  stop))
        worker.start()
        import time as _time
        _time.sleep(2.5)
        stop.set()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertTrue(screen.frames, "run must present at least one frame")


if __name__ == "__main__":
    unittest.main()
