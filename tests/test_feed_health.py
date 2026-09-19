"""Feed-health classification + feed_health renderer tests.

No framebuffer needed except through the patched DisplayDaemon used for
the feed-error path. The renderer tests paint into an in-memory fake.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import importlib.util
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
from displayd import FeedStore


def load_feed_health(name="feed_health_mod"):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(displayd.RENDERER_DIR, "feed_health.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHAT_SPEC = {"type": "object", "required": ["author", "text"],
             "properties": {"author": {"type": "string"},
                            "text": {"type": "string"}}}


class TestClassifyHealth(unittest.TestCase):
    def test_never_received_is_cold(self):
        self.assertEqual(FeedStore.classify_health(None), "cold")

    def test_recent_push_is_warm(self):
        now = time.time()
        self.assertEqual(FeedStore.classify_health(now - 10, None, now), "warm")

    def test_old_push_is_stale(self):
        now = time.time()
        self.assertEqual(
            FeedStore.classify_health(now - FeedStore.STALE_AFTER - 1, None, now),
            "stale")

    def test_failed_attempt_with_no_values_is_error(self):
        now = time.time()
        self.assertEqual(
            FeedStore.classify_health(None, now - 5, now), "error")

    def test_failure_after_last_success_is_error(self):
        now = time.time()
        self.assertEqual(
            FeedStore.classify_health(now - 60, now - 5, now), "error")

    def test_success_after_failure_is_warm_again(self):
        now = time.time()
        self.assertEqual(
            FeedStore.classify_health(now - 5, now - 60, now), "warm")


class TestFeedStoreErrors(unittest.TestCase):
    def test_declare_starts_cold_without_error(self):
        store = FeedStore()
        store.declare("chat", "message", CHAT_SPEC)
        snap = store.snapshot()["chat"]["message"]
        self.assertEqual(snap["health"], "cold")
        self.assertIsNone(snap["last_error"])

    def test_note_error_marks_feed_error(self):
        store = FeedStore()
        store.declare("chat", "message", CHAT_SPEC)
        store.note_error("chat", "message", "boom")
        snap = store.snapshot()["chat"]["message"]
        self.assertEqual(snap["health"], "error")
        self.assertEqual(snap["last_error"], "boom")

    def test_successful_push_clears_error(self):
        store = FeedStore()
        store.declare("chat", "message", CHAT_SPEC)
        store.note_error("chat", "message", "boom")
        store.push("chat", "message", {"author": "a", "text": "hi"}, CHAT_SPEC)
        snap = store.snapshot()["chat"]["message"]
        self.assertEqual(snap["health"], "warm")
        self.assertIsNone(snap["last_error"])

    def test_failed_push_preserves_buffer_and_marks_error_via_daemon(self):
        real_fb = displayd.Framebuffer

        class TinyFb(real_fb):
            def __init__(self):
                self.width, self.height = 96, 48
                self.blanked = False
                self.backlight = None

            def take_console(self):
                return False

            def set_blank(self, blank):
                self.blanked = bool(blank)

        displayd.Framebuffer = TinyFb
        try:
            daemon = displayd.DisplayDaemon()
            try:
                daemon.renderers["chat"] = {"module": object(),
                                            "description": "t",
                                            "params": {},
                                            "inputs": {"message": CHAT_SPEC},
                                            "static": True}
                daemon.feeds.declare("chat", "message", CHAT_SPEC)
                daemon.feed("chat", "message", {"author": "a", "text": "ok"})
                self.assertEqual(
                    daemon.feeds.snapshot()["chat"]["message"]["health"], "warm")
                with self.assertRaises(ValueError):
                    daemon.feed("chat", "message", {"author": "only"})
                snap = daemon.feeds.snapshot()["chat"]["message"]
                self.assertEqual(snap["health"], "error")
                # The good value is still buffered: errors never drop data.
                self.assertEqual(
                    daemon.feeds.get("chat", "message")[0]["text"], "ok")
                # Recovery: a good push returns the feed to warm.
                daemon.feed("chat", "message", {"author": "a", "text": "back"})
                self.assertEqual(
                    daemon.feeds.snapshot()["chat"]["message"]["health"], "warm")
            finally:
                daemon.stop_watchdog()
        finally:
            displayd.Framebuffer = real_fb

    def test_snapshot_stays_backward_compatible(self):
        store = FeedStore()
        store.declare("chat", "message", CHAT_SPEC)
        store.push("chat", "message", {"author": "a", "text": "hi"}, CHAT_SPEC)
        snap = store.snapshot()["chat"]["message"]
        for key in ("count", "updated_at", "age_seconds", "health"):
            self.assertIn(key, snap)
        one = store.status("chat", "message")
        for key in ("renderer", "input", "count", "updated_at",
                    "age_seconds", "health", "latest"):
            self.assertIn(key, one)


class FakeFb:
    def __init__(self, w=960, h=540):
        self.width, self.height = w, h
        self.frames = []

    def present(self, img):
        self.frames.append(img.copy())


class TestFeedHealthHelpers(unittest.TestCase):
    def setUp(self):
        self.mod = load_feed_health("fh_helpers")

    def test_format_age(self):
        self.assertEqual(self.mod.format_age(None), "never")
        self.assertEqual(self.mod.format_age(12.4), "12s")
        self.assertEqual(self.mod.format_age(180), "3m")
        self.assertEqual(self.mod.format_age(7200), "2h")
        self.assertEqual(self.mod.format_age(86400 * 3), "3d")

    def test_collect_rows_sorts_and_colors(self):
        snap = {"chat": {"message": {"count": 2, "updated_at": 1,
                                     "age_seconds": 5, "health": "warm"}},
                "beads": {"detail": {"count": 0, "updated_at": None,
                                     "age_seconds": None, "health": "cold"}}}
        rows = self.mod.collect_rows(snap)
        self.assertEqual([r["name"] for r in rows],
                         ["beads.detail", "chat.message"])
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["chat.message"]["color"], (80, 220, 120))
        self.assertEqual(by_name["beads.detail"]["color"], (128, 128, 128))
        self.assertEqual(by_name["chat.message"]["age"], "5s")
        self.assertEqual(by_name["beads.detail"]["age"], "never")

    def test_summarize(self):
        text, color = self.mod.summarize([])
        self.assertIn("NO FEEDS", text)
        text, color = self.mod.summarize(
            [{"health": "warm"}, {"health": "warm"}])
        self.assertIn("ALL HEALTHY", text)
        self.assertEqual(color, (80, 220, 120))
        text, color = self.mod.summarize(
            [{"health": "warm"}, {"health": "stale"}])
        self.assertIn("DEGRADED", text)
        self.assertEqual(color, (240, 200, 60))
        text, color = self.mod.summarize(
            [{"health": "warm"}, {"health": "error"}])
        self.assertIn("DEGRADED", text)
        self.assertEqual(color, (255, 80, 80))

    def test_health_colors_match_contract(self):
        self.assertEqual(self.mod.HEALTH_COLORS["warm"], (80, 220, 120))
        self.assertEqual(self.mod.HEALTH_COLORS["stale"], (240, 200, 60))
        self.assertEqual(self.mod.HEALTH_COLORS["error"], (255, 80, 80))
        self.assertEqual(self.mod.HEALTH_COLORS["cold"], (128, 128, 128))


class TestFeedHealthRenderer(unittest.TestCase):
    def make_screen(self):
        screen = displayd.Screen(FakeFb())
        screen.feeds = FeedStore()
        return screen

    def test_draw_is_screen_sized(self):
        mod = load_feed_health("fh_draw")
        screen = self.make_screen()
        rows = mod.collect_rows({"chat": {"message": {
            "count": 1, "updated_at": time.time(),
            "age_seconds": 3, "health": "warm"}}})
        summary, color = mod.summarize(rows)
        img = mod._draw(screen, "FEED HEALTH", rows, summary, color, (10, 10, 14))
        self.assertEqual(img.size, (screen.W, screen.H))

    def test_draw_empty_snapshot(self):
        mod = load_feed_health("fh_empty")
        screen = self.make_screen()
        summary, color = mod.summarize([])
        img = mod._draw(screen, "FEED HEALTH", [], summary, color, (10, 10, 14))
        self.assertEqual(img.size, (screen.W, screen.H))
        small = img.resize((160, 90)).convert("L")
        self.assertGreater(sum(1 for p in small.getdata() if p > 24), 20)

    def test_run_paints_feeds_and_refreshes(self):
        mod = load_feed_health("fh_run")
        mod.POLL = 0.2
        screen = self.make_screen()
        screen.feeds.declare("chat", "message", CHAT_SPEC)
        stop = threading.Event()
        thread = threading.Thread(target=mod.run,
                                  args=(screen, {}, stop), daemon=True)
        thread.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and not screen.fb.frames:
                time.sleep(0.05)
            self.assertTrue(screen.fb.frames, "feed_health never painted")
            first = len(screen.fb.frames)
            # A failed update while selected surfaces as red/error on refresh.
            screen.feeds.note_error("chat", "message", "bad payload")
            deadline = time.time() + 5
            while time.time() < deadline and len(screen.fb.frames) < first + 1:
                time.sleep(0.05)
            self.assertGreater(len(screen.fb.frames), first,
                               "feed_health did not auto-refresh")
        finally:
            stop.set()
            thread.join(timeout=5)

    def test_renderer_is_discoverable(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("feed_health", found)
        entry = found["feed_health"]
        self.assertEqual(entry["inputs"], {})
        self.assertFalse(entry["static"])


if __name__ == "__main__":
    unittest.main()
