"""Policy-layer tests: notifications, chat-attention, idle-off.

No framebuffer needed: DisplayDaemon is constructed against a fake
Framebuffer subclass that inherits the REAL power_off/power_on save/restore
logic (so the task-i0agw backlight fix is tested on the true code path)
while stubbing only the sysfs/file writes.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
import policy as policy_module


class FakeFramebuffer(displayd.Framebuffer):
    """Real power logic, fake wires: brightness/sysfs kept in memory."""

    def __init__(self, brightness=200, max_brightness=255):
        self.width, self.height, self.bpp = 96, 48, 32
        self.stride = self.width * 4
        self.fd = None
        self.last_frame = None
        self.blanked = False
        self.saved_brightness = None
        self._brightness = brightness
        self._max = max_brightness
        self.backlight = "/fake/backlight0"
        self.vt_fd = None
        self.writes = []

    def present(self, img):
        self.last_frame = (img.size, img.tobytes()[:16])

    def raw(self, data):
        self.last_frame = data

    def repaint(self):
        pass

    def _read_int(self, path):
        if path.endswith("max_brightness"):
            return self._max
        if path.endswith("brightness"):
            return self._brightness
        if path.endswith("blank"):
            return 4 if self.blanked else 0
        return None

    def set_brightness(self, value):
        value = max(0, min(int(value), self._max))
        self._brightness = value
        self.writes.append(value)
        return True

    def set_blank(self, value):
        self.blanked = (int(value) != 0)
        return True

    def get_blank(self):
        return 4 if self.blanked else 0

    def take_console(self):
        return True


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


CHAT_SPEC = {
    "message": {
        "type": "object",
        "required": ["author", "text"],
        "properties": {
            "author": {"type": "string"},
            "text": {"type": "string"},
        },
        "buffer": 60,
    },
}

CHAT_MSG = {"author": "synthetic-user", "text": "SYNTHETIC chat event for tests"}


def make_chat_module():
    """A clearly-labelled synthetic chat view (stand-in until the real chat
    renderer lands from its own branch; never committed to renderers/)."""
    mod = types.ModuleType("synthetic_chat_for_policy_tests")
    mod.NAME = "chat"
    mod.DESCRIPTION = "SYNTHETIC chat view for policy tests only"
    mod.STATIC = True
    mod.PARAMS = {}
    mod.INPUTS = CHAT_SPEC

    def run(screen, params, stop):
        screen.present(screen.new_image((0, 0, 40)))

    mod.run = run
    return mod


class PolicyTestCase(unittest.TestCase):
    def setUp(self):
        self._real_fb = displayd.Framebuffer
        displayd.Framebuffer = FakeFramebuffer
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy_path = os.path.join(self.tmp.name, "policy.json")
        self.clock = FakeClock()

    def tearDown(self):
        displayd.Framebuffer = self._real_fb

    def make_daemon(self, clock=None):
        daemon = displayd.DisplayDaemon(policy_path=self.policy_path,
                                        clock=clock or self.clock)
        daemon.renderers["chat"] = {
            "module": make_chat_module(),
            "description": "SYNTHETIC chat view for policy tests only",
            "params": {},
            "inputs": CHAT_SPEC,
            "static": True,
        }
        daemon.feeds.declare("chat", "message", CHAT_SPEC["message"])
        self.addCleanup(daemon.stop_watchdog)
        return daemon


# ---- notifications ------------------------------------------------------


class TestNotify(PolicyTestCase):
    def test_notify_shows_and_returns(self):
        daemon = self.make_daemon(clock=None)  # real clock: timers fire
        daemon.show("solid", {"color": "blue"})
        time.sleep(0.1)
        self.assertEqual(daemon.current, "solid")
        result = daemon.notify("hello", body="world", severity="info", duration=1)
        self.assertEqual(daemon.current, "notice")
        self.assertEqual(result["return_in"], 1)
        time.sleep(1.6)  # duration elapses: automatic return
        self.assertEqual(daemon.current, "solid")

    def test_notify_validates(self):
        daemon = self.make_daemon()
        with self.assertRaises(ValueError):
            daemon.notify("")
        with self.assertRaises(ValueError):
            daemon.notify("t", severity="urgent")
        with self.assertRaises(ValueError):
            daemon.notify("t", duration=9999)

    def test_manual_change_during_notice_wins(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        daemon.notify("hello", duration=60)
        token = daemon.policy.active["token"]
        daemon.show("text", {"text": "captain takes over"})
        self.assertEqual(daemon.current, "text")
        # The stale return timer fires late: it must not clobber the manual choice.
        daemon._transient_expired("notice", token)
        self.assertEqual(daemon.current, "text")

    def test_second_notice_replaces_first(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        daemon.notify("first", duration=60)
        first_token = daemon.policy.active["token"]
        daemon.notify("second", duration=60)
        self.assertEqual(daemon.current, "notice")
        self.assertNotEqual(daemon.policy.active["token"], first_token)
        daemon._transient_expired("notice", first_token)  # stale: no-op
        self.assertEqual(daemon.current, "notice")


# ---- chat attention ------------------------------------------------------


class TestChatAttention(PolicyTestCase):
    def test_off_by_default(self):
        daemon = self.make_daemon()
        cfg = daemon.policy.get_config()["chat_attention"]
        self.assertFalse(cfg["enabled"])
        daemon.show("solid", {"color": "blue"})
        result = daemon.feed("chat", "message", dict(CHAT_MSG))
        self.assertFalse(result["attention"]["switched"])
        self.assertEqual(result["attention"]["reason"], "disabled")
        self.assertEqual(daemon.current, "solid")

    def test_enabled_pulls_and_returns(self):
        daemon = self.make_daemon(clock=None)
        daemon.set_policy({"chat_attention": {"enabled": True, "return_after": 5}})
        daemon.show("solid", {"color": "blue"})
        result = daemon.feed("chat", "message", dict(CHAT_MSG))
        self.assertTrue(result["attention"]["switched"])
        self.assertEqual(daemon.current, "chat")
        time.sleep(5.6)  # return_after elapses
        self.assertEqual(daemon.current, "solid")

    def test_manual_change_during_attention_wins(self):
        daemon = self.make_daemon()
        daemon.set_policy({"chat_attention": {"enabled": True, "return_after": 60}})
        daemon.show("solid", {"color": "blue"})
        daemon.feed("chat", "message", dict(CHAT_MSG))
        self.assertEqual(daemon.current, "chat")
        token = daemon.policy.active["token"]
        daemon.show("text", {"text": "captain takes over"})
        daemon._transient_expired("attention", token)
        self.assertEqual(daemon.current, "text")

    def test_unknown_view_skips_switch_but_keeps_feed(self):
        daemon = self.make_daemon()
        # Operator points attention at a view that is not installed
        # (e.g. the real chat renderer has not landed yet).
        daemon.set_policy({"chat_attention": {"enabled": True,
                                                 "view": "chatty"}})
        daemon.show("solid", {"color": "blue"})
        result = daemon.feed("chat", "message", dict(CHAT_MSG))
        self.assertEqual(result["attention"]["reason"], "not-a-chat-event")
        self.assertEqual(daemon.current, "solid")
        self.assertEqual(len(daemon.feeds.get("chat", "message")), 1)

    def test_broken_view_entry_rejects_feed_cleanly(self):
        daemon = self.make_daemon()
        daemon.set_policy({"chat_attention": {"enabled": True}})
        daemon.show("solid", {"color": "blue"})
        daemon.renderers["chat"] = {"broken": "simulated bad import"}
        # A chat event to an uninstallable view is a clean 404-style
        # KeyError, not a crash, and the panel is undisturbed.
        with self.assertRaises(KeyError):
            daemon.feed("chat", "message", dict(CHAT_MSG))
        self.assertEqual(daemon.current, "solid")

    def test_non_chat_feed_never_pulls(self):
        daemon = self.make_daemon()
        daemon.set_policy({"chat_attention": {"enabled": True,
                                                 "return_after": 60}})
        daemon.show("solid", {"color": "blue"})
        result = daemon.feed("beads-detail", "focus", {"bead_id": "task-a"})
        self.assertEqual(result["attention"]["reason"], "not-a-chat-event")
        self.assertEqual(daemon.current, "solid")


# ---- priority ------------------------------------------------------------


class TestPriority(PolicyTestCase):
    def test_notice_preempts_attention_and_returns_to_base(self):
        daemon = self.make_daemon()
        daemon.set_policy({"chat_attention": {"enabled": True, "return_after": 60}})
        daemon.show("solid", {"color": "blue"})
        daemon.feed("chat", "message", dict(CHAT_MSG))
        self.assertEqual(daemon.current, "chat")
        out = daemon.notify("urgent", severity="critical", duration=60)
        self.assertEqual(out.get("superseded"), "attention")
        self.assertEqual(daemon.current, "notice")
        token = daemon.policy.active["token"]
        daemon._transient_expired("notice", token)
        self.assertEqual(daemon.current, "solid")  # base, not chat

    def test_attention_suppressed_while_notice_active(self):
        daemon = self.make_daemon()
        daemon.set_policy({"chat_attention": {"enabled": True, "return_after": 60}})
        daemon.show("solid", {"color": "blue"})
        daemon.notify("reading", duration=60)
        result = daemon.feed("chat", "message", dict(CHAT_MSG))
        self.assertFalse(result["attention"]["switched"])
        self.assertEqual(result["attention"]["reason"], "notice-active")
        self.assertEqual(daemon.current, "notice")
        # ...but the message itself is still buffered for the chat view.
        self.assertEqual(len(daemon.feeds.get("chat", "message")), 1)


# ---- idle -----------------------------------------------------------------


class TestIdle(PolicyTestCase):
    def test_off_by_default(self):
        daemon = self.make_daemon()
        self.assertFalse(daemon.policy.get_config()["idle"]["enabled"])
        self.clock.advance(3600)
        daemon.check_idle()
        self.assertFalse(daemon.fb.blanked)

    def test_idle_blanks_and_activity_wakes(self):
        daemon = self.make_daemon()
        daemon.set_policy({"idle": {"enabled": True, "after_seconds": 60}})
        daemon.show("solid", {"color": "blue"})
        self.assertEqual(daemon.fb._brightness, 200)
        self.clock.advance(61)
        daemon.check_idle()
        self.assertTrue(daemon.fb.blanked)
        self.assertTrue(daemon.policy.idle_off)
        self.assertEqual(daemon.fb._brightness, 0)
        # Feed activity wakes the panel and restores a readable brightness.
        daemon.feed("chat", "message", dict(CHAT_MSG))
        self.assertFalse(daemon.fb.blanked)
        self.assertFalse(daemon.policy.idle_off)
        self.assertEqual(daemon.fb._brightness, 200)
        self.assertEqual(daemon.current, "solid")  # content undisturbed

    def test_idle_skipped_while_transient_active(self):
        daemon = self.make_daemon()
        daemon.set_policy({"idle": {"enabled": True, "after_seconds": 60}})
        daemon.show("solid", {"color": "blue"})
        daemon.notify("hello", duration=60)
        self.clock.advance(3600)
        daemon.check_idle()
        self.assertFalse(daemon.fb.blanked)

    def test_manual_power_off_is_not_auto_woken(self):
        daemon = self.make_daemon()
        daemon.set_policy({"idle": {"enabled": True, "after_seconds": 60}})
        daemon.show("solid", {"color": "blue"})
        daemon.set_power("off")
        self.assertTrue(daemon.fb.blanked)
        daemon.feed("chat", "message", dict(CHAT_MSG))  # not idle-off: no wake
        self.assertTrue(daemon.fb.blanked)


# ---- backlight save/restore (task-i0agw) -----------------------------------


class TestBacklightRestore(PolicyTestCase):
    def test_dimmed_value_is_never_captured(self):
        daemon = self.make_daemon()
        fb = daemon.fb
        fb._brightness = 200
        fb.power_off()
        self.assertEqual(fb.saved_brightness, 200)
        fb._brightness = 200  # panel back on at a readable level...
        fb.power_on()
        fb._brightness = 5  # ...then dimmed by hand...
        fb.power_off()  # ...then powered off: the dim 5 must NOT stick.
        self.assertEqual(fb.saved_brightness, 200)

    def test_restore_floors_to_max(self):
        daemon = self.make_daemon()
        fb = daemon.fb
        fb.saved_brightness = 5  # poisoned state from the old bug
        fb._brightness = 0
        fb.power_on()
        self.assertEqual(fb._brightness, fb._max)
        self.assertGreater(fb._brightness, 100)

    def test_round_trip_restores_sensible_level(self):
        daemon = self.make_daemon()
        fb = daemon.fb
        fb._brightness = 200
        fb.power_off()
        self.assertEqual(fb._brightness, 0)
        fb.power_on()
        self.assertEqual(fb._brightness, 200)


# ---- config surface + persistence -------------------------------------------


class TestPolicyConfig(PolicyTestCase):
    def test_update_and_reload_survives_restart(self):
        daemon = self.make_daemon()
        daemon.set_policy({"idle": {"enabled": True, "after_seconds": 90},
                           "chat_attention": {"enabled": True}})
        with open(self.policy_path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertTrue(stored["idle"]["enabled"])
        self.assertEqual(stored["idle"]["after_seconds"], 90)
        # A fresh Policy on the same file picks it up: restart-survival.
        reloaded = policy_module.Policy(self.policy_path, clock=self.clock)
        self.assertTrue(reloaded.get_config()["idle"]["enabled"])
        self.assertTrue(reloaded.get_config()["chat_attention"]["enabled"])

    def test_rejects_bad_patches(self):
        daemon = self.make_daemon()
        for patch in ({"nope": {}}, {"idle": {"bogus": 1}},
                      {"idle": {"after_seconds": 1}},  # below min
                      {"idle": {"enabled": "yes"}},  # not a bool
                      {"chat_attention": {"view": ""}},  # empty
                      {"notifications": {"default_duration": 9999}}):
            with self.assertRaises(ValueError, msg="patch %r" % (patch,)):
                daemon.set_policy(patch)

    def test_get_policy_reports_clock_and_transient(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        snap = daemon.get_policy()
        self.assertIn("config", snap)
        self.assertIn("activity", snap)
        self.assertIn("transient", snap)
        self.assertIsNone(snap["transient"]["active"])
        daemon.notify("hi", duration=60)
        self.assertEqual(daemon.get_policy()["transient"]["active"], "notice")


# ---- feeds -------------------------------------------------------------------


class TestFeeds(PolicyTestCase):
    def test_push_validate_buffer(self):
        daemon = self.make_daemon()
        out = daemon.feed("chat", "message", dict(CHAT_MSG))
        self.assertEqual(out["feed"]["count"], 1)
        self.assertEqual(daemon.feeds.get("chat", "message"), [CHAT_MSG])
        with self.assertRaises(ValueError):
            daemon.feed("chat", "message", {"author": "x"})  # missing text
        with self.assertRaises(KeyError):
            daemon.feed("chat", "nope", {})
        with self.assertRaises(KeyError):
            daemon.feed("nope", "message", {})

    def test_renderers_advertise_inputs(self):
        daemon = self.make_daemon()
        by_name = {r["name"]: r for r in daemon.renderer_list()}
        self.assertIn("message", by_name["chat"]["inputs"])
        self.assertIn("focus", by_name["beads-detail"]["inputs"])
        self.assertIn("feeds", daemon.state())

    def test_control_page_exposes_policy(self):
        page = displayd.CONTROL_PAGE
        # The page drives policy + notify; feeds arrive from bridges,
        # not from the control UI, so /feed/ is deliberately absent.
        for endpoint in ("/policy", "/notify"):
            self.assertIn(endpoint, page, "page never talks to %s" % endpoint)
        lowered = page.lower()
        for token in ("chat attention", "inactivity", "notice"):
            self.assertIn(token, lowered, "page has no %r control" % token)


# ---- existing views still work -------------------------------------------------


class TestExistingViews(PolicyTestCase):
    def _run_view(self, renderer_mod, params, timeout=15.0):
        from PIL import Image

        class Screen:
            W, H = 1920, 1080

            def __init__(self):
                self.frames = []

            def new_image(self, background=(0, 0, 0)):
                return Image.new("RGB", (self.W, self.H), background)

            def present(self, img):
                self.frames.append(img.copy())

            def clear(self, background=(0, 0, 0)):
                self.present(self.new_image(background))

            def get_input(self, renderer, name):
                return []

            color = staticmethod(displayd.Screen.color)
            font_path = staticmethod(displayd.Screen.font_path)

        screen = Screen()
        stop = threading.Event()
        thread = threading.Thread(target=renderer_mod.run,
                                  args=(screen, params, stop), daemon=True)
        thread.start()
        deadline = time.time() + timeout
        while not screen.frames and time.time() < deadline:
            time.sleep(0.1)
        stop.set()
        thread.join(timeout=5.0)
        return screen

    def test_beads_overview_renders(self):
        from renderers import beads
        mirror = os.path.join(self.tmp.name, "mirror.json")
        with open(mirror, "w", encoding="utf-8") as fh:
            json.dump([{"id": "task-a", "title": "rolling work",
                        "status": "in_progress", "priority": 1,
                        "labels": [], "dependencies": []}], fh)
        screen = self._run_view(beads, {"mirror": mirror, "stores": ""})
        self.assertTrue(screen.frames, "beads overview drew no frame")
        self.assertEqual(screen.frames[0].size, (1920, 1080))

    def test_bead_detail_card_renders(self):
        from renderers import beads_detail as detail
        mirror = os.path.join(self.tmp.name, "mirror.json")
        with open(mirror, "w", encoding="utf-8") as fh:
            json.dump([{"id": "task-a", "title": "detail work",
                        "status": "open", "priority": 1,
                        "labels": [], "dependencies": []}], fh)
        screen = self._run_view(detail, {"mirror": mirror, "stores": "",
                                         "focus": "task-a"})
        self.assertTrue(screen.frames, "beads-detail drew no frame")
        self.assertEqual(screen.frames[0].size, (1920, 1080))

    def test_notice_renders_with_severity_bar(self):
        from renderers import notice
        screen = self._run_view(notice, {"title": "hello", "body": "world",
                                         "severity": "critical"})
        self.assertTrue(screen.frames, "notice drew no frame")
        pixels = screen.frames[0].load()
        self.assertEqual(pixels[960, 9], (255, 70, 70))  # severity bar


if __name__ == "__main__":
    unittest.main()
