"""Reload-confirmation tests: renderer, transient policy, HTTP endpoint.

The reload screen must show RELOADED plus the full deployed SHA, and its
QR code must decode to exactly the commit page
https://github.com/trillium/displayd/commit/<full-sha> -- never a
caller-supplied URL. POST /reload is a transient like POST /notify: the
prior view returns automatically and a manual /show wins over a late
timer. Missing, malformed, or excessive SHA/duration input is a clear 400.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd

from renderers import reload as reload_view
from renderers import qr_common

# The deployed lnx-server commit from the launch brief: the canonical
# example SHA used across these tests.
DEPLOYED_SHA = "25e0e740074740b6b98896a6076bf2763fe598f1"
OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"
COMMIT_URL = ("https://github.com/trillium/displayd/commit/" + DEPLOYED_SHA)


def decode_png(png_bytes):
    """Independent decode: returns the decoded string or None."""
    try:
        import cv2
        import numpy as np
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        data, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        if data:
            return data
    except ImportError:
        pass
    try:
        from pyzbar.pyzbar import decode as zbar_decode
        from PIL import Image
        found = zbar_decode(Image.open(io.BytesIO(png_bytes)))
        if found:
            return found[0].data.decode("utf-8")
    except ImportError:
        pass
    return None


def decoder_available():
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import pyzbar  # noqa: F401
        return True
    except ImportError:
        return False


class FakeScreen:
    W, H = 1920, 1080

    def __init__(self):
        self.frames = []

    def new_image(self, background=(0, 0, 0)):
        from PIL import Image
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


def run_reload(params):
    screen = FakeScreen()
    reload_view.run(screen, params, threading.Event())
    assert screen.frames, "reload.run presented nothing"
    return screen.frames[-1]


def to_png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def drawn_strings(params):
    """Render while recording every string handed to PIL text drawing."""
    import PIL.ImageDraw as PILDraw
    real_text = PILDraw.ImageDraw.text
    real_mtext = PILDraw.ImageDraw.multiline_text
    drawn = []

    def rec_text(self, xy, text, *args, **kwargs):
        drawn.append(str(text))
        return real_text(self, xy, text, *args, **kwargs)

    def rec_mtext(self, xy, text, *args, **kwargs):
        drawn.append(str(text))
        return real_mtext(self, xy, text, *args, **kwargs)

    PILDraw.ImageDraw.text = rec_text
    PILDraw.ImageDraw.multiline_text = rec_mtext
    try:
        run_reload(params)
    finally:
        PILDraw.ImageDraw.text = real_text
        PILDraw.ImageDraw.multiline_text = real_mtext
    return drawn


class TestReloadContract(unittest.TestCase):
    def test_renderer_loads_and_advertises(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("reload", found)
        self.assertIn("module", found["reload"], found["reload"].get("broken"))
        mod = found["reload"]["module"]
        self.assertTrue(mod.STATIC)
        self.assertTrue(callable(mod.run))
        self.assertTrue(mod.PARAMS["sha"].get("required"))
        self.assertEqual(mod.PARAMS["sha"]["type"], "string")

    def test_commit_url_rule_agrees_between_daemon_and_renderer(self):
        self.assertEqual(displayd.RELOAD_COMMIT_URL_PREFIX,
                         reload_view.COMMIT_URL_PREFIX)
        self.assertEqual(displayd.RELOAD_SHA_RE.pattern,
                         reload_view.SHA_RE.pattern)
        self.assertEqual(reload_view.commit_url(DEPLOYED_SHA), COMMIT_URL)
        self.assertEqual(displayd.RELOAD_COMMIT_URL_PREFIX + DEPLOYED_SHA,
                         COMMIT_URL)

    def test_visible_text_is_reloaded_and_full_sha(self):
        drawn = drawn_strings({"sha": DEPLOYED_SHA})
        self.assertTrue(any("RELOADED" in s for s in drawn),
                        "RELOADED headline was never drawn: %r" % (drawn,))
        self.assertTrue(any(DEPLOYED_SHA in s for s in drawn),
                        "full SHA was never drawn: %r" % (drawn,))

    def test_frame_changes_with_sha_but_is_deterministic(self):
        first = run_reload({"sha": DEPLOYED_SHA})
        same = run_reload({"sha": DEPLOYED_SHA})
        other = run_reload({"sha": OTHER_SHA})
        self.assertEqual(first.tobytes(), same.tobytes(),
                         "same SHA must render the same frame")
        self.assertNotEqual(first.tobytes(), other.tobytes(),
                            "the SHA is on screen: changing it must change pixels")

    def test_no_arbitrary_qr_url_from_params(self):
        """Extra URL-ish params are ignored: the QR is always the commit page."""
        img = run_reload({"sha": DEPLOYED_SHA,
                          "data": "https://evil.example/x",
                          "url": "https://evil.example/x",
                          "caption": "https://evil.example/x"})
        self.assertEqual(img.size, (1920, 1080))
        if decoder_available():
            self.assertEqual(decode_png(to_png(img)), COMMIT_URL)

    def test_bad_sha_renders_a_message_never_blank(self):
        for bad in ("", "25e0e74", "x" * 40):
            img = run_reload({"sha": bad})
            self.assertEqual(img.size, (1920, 1080))
            self.assertTrue(any(img.tobytes()),
                            "bad SHA %r must message, not blank" % (bad,))


@unittest.skipUnless(decoder_available(),
                     "no independent QR decoder installed (cv2/pyzbar)")
class TestReloadQrPayload(unittest.TestCase):
    def test_qr_decodes_to_exact_commit_url(self):
        img = run_reload({"sha": DEPLOYED_SHA})
        self.assertEqual(img.size, (1920, 1080))
        self.assertEqual(decode_png(to_png(img)), COMMIT_URL)

    def test_qr_identifies_commit_not_homepage(self):
        payload = decode_png(to_png(run_reload({"sha": DEPLOYED_SHA})))
        self.assertTrue(payload.startswith(
            "https://github.com/trillium/displayd/commit/"))
        self.assertTrue(payload.endswith(DEPLOYED_SHA))
        self.assertNotEqual(payload, "https://github.com/trillium/displayd")


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
    mod = types.ModuleType("synthetic_chat_for_reload_tests")
    mod.NAME = "chat"
    mod.DESCRIPTION = "SYNTHETIC chat view for reload tests only"
    mod.STATIC = True
    mod.PARAMS = {}
    mod.INPUTS = CHAT_SPEC

    def run(screen, params, stop):
        screen.present(screen.new_image((0, 0, 40)))

    mod.run = run
    return mod


class ReloadDaemonTestCase(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def make_daemon(self, clock=None):
        daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            clock=clock)
        daemon.renderers["chat"] = {
            "module": make_chat_module(),
            "description": "SYNTHETIC chat view for reload tests only",
            "params": {},
            "inputs": CHAT_SPEC,
            "static": True,
        }
        for name, spec in CHAT_SPEC.items():
            daemon.feeds.declare("chat", name, spec)
        self.addCleanup(daemon.stop_watchdog)
        return daemon


class TestReloadTransient(ReloadDaemonTestCase):
    def test_reload_shows_indefinitely(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        result = daemon.reload(DEPLOYED_SHA)
        self.assertEqual(daemon.current, "reload")
        self.assertEqual(result["view"], "reload")
        self.assertEqual(result["params"], {"sha": DEPLOYED_SHA})
        self.assertEqual(result["commit_url"], COMMIT_URL)
        self.assertIsNone(result["return_in"])  # indefinite: no auto-return
        self.assertEqual(daemon.policy.transient_status()["active"], "reload")
        self.assertIsNone(daemon.policy.transient_status()["in_seconds"])
        self.assertIsNone(daemon.transient_timer)  # no return timer armed

    def test_reload_ignores_legacy_duration_but_validates_it(self):
        # Old clients still sending duration keep working; the value is
        # accepted and ignored -- the screen stays until a tap.
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        result = daemon.reload(DEPLOYED_SHA, duration=60)
        self.assertIsNone(result["return_in"])
        self.assertIsNone(daemon.transient_timer)
        self.assertEqual(daemon.current, "reload")

    def test_reload_never_returns_on_its_own(self):
        daemon = self.make_daemon(clock=None)  # real clock: timers would fire
        daemon.show("solid", {"color": "blue"})
        time.sleep(0.1)
        daemon.reload(DEPLOYED_SHA)
        self.assertEqual(daemon.current, "reload")
        time.sleep(1.6)  # past every historical default: still showing
        self.assertEqual(daemon.current, "reload")
        self.assertEqual(daemon.policy.transient_status()["active"], "reload")

    def test_tap_dismiss_returns_to_saved_base(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        daemon.reload(DEPLOYED_SHA)
        self.assertEqual(daemon.current, "reload")
        out = daemon.dismiss_reload()
        self.assertTrue(out["dismissed"])
        self.assertEqual(daemon.current, "solid")
        self.assertEqual(out["view"], "solid")
        self.assertIsNone(daemon.policy.transient_status()["active"])

    def test_tap_dismiss_without_base_view_returns_to_clock(self):
        # Fresh restart: no explicit base view. The tap return must still
        # fall back to the clock instead of a blank panel.
        daemon = self.make_daemon()
        self.assertIsNone(daemon.policy.base)
        result = daemon.reload(DEPLOYED_SHA)
        self.assertEqual(daemon.current, "reload")
        self.assertEqual(result["view"], "reload")
        out = daemon.dismiss_reload()
        self.assertTrue(out["dismissed"])
        self.assertEqual(daemon.current, "clock")

    def test_tap_dismiss_non_reload_is_noop(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        out = daemon.dismiss_reload()
        self.assertFalse(out["dismissed"])
        self.assertEqual(daemon.current, "solid")
        # A notice is not a reload: the tap must leave it alone.
        daemon.notify("operator note", duration=60)
        self.assertEqual(daemon.current, "notice")
        out = daemon.dismiss_reload()
        self.assertFalse(out["dismissed"])
        self.assertEqual(daemon.current, "notice")
        self.assertEqual(daemon.policy.transient_status()["active"], "notice")

    def test_repeated_taps_are_safe(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        daemon.reload(DEPLOYED_SHA)
        first = daemon.dismiss_reload()
        self.assertTrue(first["dismissed"])
        for _ in range(3):
            out = daemon.dismiss_reload()
            self.assertFalse(out["dismissed"])
            self.assertEqual(daemon.current, "solid")
        # Tapping with nothing showing at all is equally harmless.
        daemon.clear()
        out = daemon.dismiss_reload()
        self.assertFalse(out["dismissed"])

    def test_stale_timer_and_token_cannot_clobber(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        daemon.reload(DEPLOYED_SHA)
        token = daemon.policy.active["token"]
        daemon.dismiss_reload()
        self.assertEqual(daemon.current, "solid")
        # A legacy return timer firing late with the old token: no-op.
        daemon._transient_expired("reload", token)
        self.assertEqual(daemon.current, "solid")
        # A forged token for the same kind: no-op as well.
        daemon.reload(OTHER_SHA)
        daemon._transient_expired("reload", token)  # previous generation
        self.assertEqual(daemon.current, "reload")
        self.assertEqual(daemon.policy.transient_status()["active"], "reload")

    def test_manual_show_during_no_base_reload_wins(self):
        daemon = self.make_daemon()
        daemon.reload(DEPLOYED_SHA)
        token = daemon.policy.active["token"]
        daemon.show("text", {"text": "captain takes over"})
        self.assertEqual(daemon.current, "text")
        # The stale return timer fires late: it must not clobber the
        # manual choice with the clock fallback.
        daemon._transient_expired("reload", token)
        self.assertEqual(daemon.current, "text")
        # Nor may a tap resurrect the cancelled reload.
        out = daemon.dismiss_reload()
        self.assertFalse(out["dismissed"])
        self.assertEqual(daemon.current, "text")

    def test_default_is_indefinite(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        result = daemon.reload(DEPLOYED_SHA)
        self.assertIsNone(result["return_in"])
        self.assertIsNone(displayd.RELOAD_DEFAULT_DURATION)

    def test_manual_change_during_reload_wins(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        daemon.reload(DEPLOYED_SHA)
        token = daemon.policy.active["token"]
        daemon.show("text", {"text": "captain takes over"})
        self.assertEqual(daemon.current, "text")
        # The stale return timer fires late: it must not clobber the manual choice.
        daemon._transient_expired("reload", token)
        self.assertEqual(daemon.current, "text")

    def test_second_reload_replaces_first(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        daemon.reload(DEPLOYED_SHA)
        first_token = daemon.policy.active["token"]
        daemon.reload(OTHER_SHA)
        self.assertEqual(daemon.current, "reload")
        self.assertNotEqual(daemon.policy.active["token"], first_token)
        daemon._transient_expired("reload", first_token)  # stale: no-op
        self.assertEqual(daemon.current, "reload")

    def test_notice_and_reload_newest_wins(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        daemon.reload(DEPLOYED_SHA, duration=60)
        reload_token = daemon.policy.active["token"]
        out = daemon.notify("operator note", duration=60)
        self.assertEqual(out["superseded"], "reload")
        self.assertEqual(daemon.current, "notice")
        daemon._transient_expired("reload", reload_token)  # stale: no-op
        self.assertEqual(daemon.current, "notice")
        out = daemon.reload(OTHER_SHA, duration=60)
        self.assertEqual(out["superseded"], "notice")
        self.assertEqual(daemon.current, "reload")

    def test_attention_suppressed_while_reload_active(self):
        daemon = self.make_daemon()
        daemon.set_policy({"chat_attention": {"enabled": True,
                                              "return_after": 60}})
        daemon.show("solid", {"color": "blue"})
        daemon.reload(DEPLOYED_SHA, duration=60)
        result = daemon.feed("chat", "message", dict(CHAT_MSG))
        self.assertFalse(result["attention"]["switched"])
        self.assertEqual(result["attention"]["reason"], "reload-active")
        self.assertEqual(daemon.current, "reload")

    def test_notice_suppression_reason_unchanged(self):
        daemon = self.make_daemon()
        daemon.set_policy({"chat_attention": {"enabled": True,
                                              "return_after": 60}})
        daemon.show("solid", {"color": "blue"})
        daemon.notify("reading", duration=60)
        result = daemon.feed("chat", "message", dict(CHAT_MSG))
        self.assertFalse(result["attention"]["switched"])
        self.assertEqual(result["attention"]["reason"], "notice-active")

    def test_uppercase_sha_normalised(self):
        daemon = self.make_daemon()
        result = daemon.reload(DEPLOYED_SHA.upper(), duration=60)
        self.assertEqual(result["params"], {"sha": DEPLOYED_SHA})
        self.assertEqual(result["commit_url"], COMMIT_URL)


class TestReloadValidation(ReloadDaemonTestCase):
    def test_missing_sha_rejected(self):
        daemon = self.make_daemon()
        for bad in (None, "", "   "):
            with self.assertRaises(ValueError, msg="sha %r" % (bad,)):
                daemon.reload(bad)

    def test_malformed_sha_rejected(self):
        daemon = self.make_daemon()
        for bad in ("25e0e74",                     # short SHA: not exact
                    DEPLOYED_SHA[:39],              # one short
                    "main",                        # branch name
                    "https://github.com/trillium/displayd",
                    "x" * 40,                      # 40 chars, not hex
                    "z" * 40,
                    25,                          # not a string (numeric)
                    ["25e0e74"]):
            with self.assertRaises(ValueError, msg="sha %r" % (bad,)):
                daemon.reload(bad)

    def test_excessive_sha_rejected(self):
        daemon = self.make_daemon()
        for bad in (DEPLOYED_SHA + "aa",            # too long
                    "x" * 4000,
                    DEPLOYED_SHA * 2):
            with self.assertRaises(ValueError, msg="sha %r" % (bad,)):
                daemon.reload(bad)

    def test_bad_duration_rejected(self):
        daemon = self.make_daemon()
        for bad in (0, -5, 301, 9999, "soon", "abc", [10]):
            with self.assertRaises(ValueError, msg="duration %r" % (bad,)):
                daemon.reload(DEPLOYED_SHA, duration=bad)

    def test_missing_renderer_is_404_style(self):
        daemon = self.make_daemon()
        daemon.renderers["reload"] = {"broken": "simulated bad import"}
        with self.assertRaises(KeyError):
            daemon.reload(DEPLOYED_SHA)


class HttpReloadTestCase(unittest.TestCase):
    """End to end over real HTTP against the in-memory framebuffer."""

    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_env)
        self._old_daemon = displayd.DAEMON
        self.addCleanup(self._restore_daemon)
        daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"),
            feedback_path=os.path.join(self.tmp.name, "feedback.jsonl"))
        displayd.DAEMON = daemon
        server = ThreadingHTTPServer(("127.0.0.1", 0), displayd.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.base = "http://127.0.0.1:%d" % server.server_address[1]
        self.daemon = daemon

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def _restore_daemon(self):
        displayd.DAEMON = self._old_daemon

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "image/" in ctype:
                    return resp.status, raw
                return resp.status, json.loads(raw.decode() or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def _wait_for_frame(self, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            code, _ = self.call("GET", "/snapshot")
            if code == 200:
                return True
            time.sleep(0.05)
        return False

    def test_reload_endpoint_shows_transient(self):
        code, _ = self.call("POST", "/show",
                            {"renderer": "clock", "params": {}})
        self.assertEqual(code, 200)
        code, result = self.call("POST", "/reload", {"sha": DEPLOYED_SHA})
        self.assertEqual(code, 200)
        self.assertEqual(result["view"], "reload")
        self.assertEqual(result["params"], {"sha": DEPLOYED_SHA})
        self.assertEqual(result["commit_url"], COMMIT_URL)
        self.assertIsNone(result["return_in"])  # indefinite: no auto-return
        code, state = self.call("GET", "/state")
        self.assertEqual(code, 200)
        self.assertEqual(state["renderer"], "reload")
        self.assertTrue(self._wait_for_frame(), "reload drew no frame")
        code, snap = self.call("GET", "/snapshot")
        self.assertEqual(code, 200)
        if decoder_available():
            self.assertEqual(decode_png(snap), COMMIT_URL)

    def test_reload_endpoint_stays_without_expiry(self):
        code, _ = self.call("POST", "/show",
                            {"renderer": "clock", "params": {}})
        self.assertEqual(code, 200)
        code, _ = self.call("POST", "/reload", {"sha": DEPLOYED_SHA})
        self.assertEqual(code, 200)
        time.sleep(1.6)  # past every historical default: still showing
        code, state = self.call("GET", "/state")
        self.assertEqual(code, 200)
        self.assertEqual(state["renderer"], "reload")

    def test_touch_tap_dismisses_reload_to_base(self):
        code, _ = self.call("POST", "/show",
                            {"renderer": "clock", "params": {}})
        self.assertEqual(code, 200)
        code, _ = self.call("POST", "/reload", {"sha": DEPLOYED_SHA})
        self.assertEqual(code, 200)
        code, out = self.call("POST", "/touch/tap", {})
        self.assertEqual(code, 200)
        self.assertTrue(out["dismissed"])
        code, state = self.call("GET", "/state")
        self.assertEqual(code, 200)
        self.assertEqual(state["renderer"], "clock")

    def test_touch_tap_without_base_returns_to_clock(self):
        # Fresh boot: no explicit base view. A tap still lands on clock.
        code, _ = self.call("POST", "/reload", {"sha": DEPLOYED_SHA})
        self.assertEqual(code, 200)
        code, out = self.call("POST", "/touch/tap", {})
        self.assertEqual(code, 200)
        self.assertTrue(out["dismissed"])
        code, state = self.call("GET", "/state")
        self.assertEqual(code, 200)
        self.assertEqual(state["renderer"], "clock")

    def test_touch_tap_non_reload_is_noop_and_repeatable(self):
        code, _ = self.call("POST", "/show",
                            {"renderer": "clock", "params": {}})
        self.assertEqual(code, 200)
        for _ in range(2):
            code, out = self.call("POST", "/touch/tap", {})
            self.assertEqual(code, 200)
            self.assertFalse(out["dismissed"])
        code, state = self.call("GET", "/state")
        self.assertEqual(code, 200)
        self.assertEqual(state["renderer"], "clock")
        # Dismiss twice after one reload: first returns, second no-ops.
        code, _ = self.call("POST", "/reload", {"sha": DEPLOYED_SHA})
        self.assertEqual(code, 200)
        code, out = self.call("POST", "/touch/tap", {})
        self.assertTrue(out["dismissed"])
        code, out = self.call("POST", "/touch/tap", {})
        self.assertEqual(code, 200)
        self.assertFalse(out["dismissed"])

    def test_reload_endpoint_rejects_bad_input(self):
        for body in (None, {},
                     {"sha": ""},
                     {"sha": "25e0e74"},
                     {"sha": "x" * 40},
                     {"sha": "x" * 4000},
                     {"sha": DEPLOYED_SHA, "duration": 9999},
                     {"sha": DEPLOYED_SHA, "duration": "soon"}):
            code, result = self.call("POST", "/reload",
                                     body if body is not None else {})
            self.assertEqual(code, 400, "body %r was not rejected" % (body,))
            self.assertIn("error", result)

    def test_reload_ignores_arbitrary_url_in_request(self):
        code, result = self.call(
            "POST", "/reload",
            {"sha": DEPLOYED_SHA, "data": "https://evil.example/x",
             "url": "https://evil.example/x"})
        self.assertEqual(code, 200)
        self.assertEqual(result["commit_url"], COMMIT_URL)

    def test_reload_appears_in_renderer_listing(self):
        code, listing = self.call("GET", "/renderers")
        self.assertEqual(code, 200)
        by_name = {r["name"]: r for r in listing["renderers"]}
        self.assertIn("reload", by_name)
        self.assertTrue(by_name["reload"]["params"]["sha"].get("required"))


# ---- scan-confirmed relay (GET /r/<token>) + tap confirm ------------------
#
# Philosophy change: the reload QR encodes a panel-served one-time relay
# URL, not the commit page. Scanning it 302-redirects the scanner to the
# commit page AND returns the panel early; a tap while the reload view
# shows confirms the same way. Tokens are single-scan with expiry == the
# reload duration, and the relay is only issued when the panel binds a
# tailnet address the captain's phone can reach (else tap-only fallback).

TAILNET_HOST = "100.81.88.113"  # the lnx-server tailnet address
TAILNET_BASE = "http://%s:8980" % TAILNET_HOST


@contextlib.contextmanager
def bind_env(host, port="8980"):
    """Override the panel bind address for relay_base_url() (read live)."""
    old_host, old_port = (os.environ.get("DISPLAYD_BIND"),
                          os.environ.get("DISPLAYD_PORT"))
    os.environ["DISPLAYD_BIND"] = host
    os.environ["DISPLAYD_PORT"] = port
    try:
        yield
    finally:
        if old_host is None:
            os.environ.pop("DISPLAYD_BIND", None)
        else:
            os.environ["DISPLAYD_BIND"] = old_host
        if old_port is None:
            os.environ.pop("DISPLAYD_PORT", None)
        else:
            os.environ["DISPLAYD_PORT"] = old_port


class TestRelayBaseUrl(unittest.TestCase):
    def test_tailnet_bind_is_reachable(self):
        with bind_env(TAILNET_HOST):
            base, reachable, reason = displayd.relay_base_url()
        self.assertTrue(reachable)
        self.assertEqual(base, TAILNET_BASE)
        self.assertIn("tailnet", reason)

    def test_loopback_bind_falls_back_to_tap_only(self):
        for host in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
            with bind_env(host):
                base, reachable, reason = displayd.relay_base_url()
            self.assertIsNone(base, host)
            self.assertFalse(reachable, host)
            self.assertIn("tap-only", reason, host)

    def test_non_tailnet_binds_are_not_reachable(self):
        for host in ("0.0.0.0", "192.168.1.10", "8.8.8.8",
                     "lnx-server", ""):
            with bind_env(host):
                base, reachable, reason = displayd.relay_base_url()
            self.assertIsNone(base, host)
            self.assertFalse(reachable, host)
            self.assertIn("tap-only", reason, host)


class TestReloadRelayIssue(ReloadDaemonTestCase):
    def test_relay_issued_on_tailnet_bind(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        with bind_env(TAILNET_HOST):
            result = daemon.reload(DEPLOYED_SHA, duration=60)
        self.assertTrue(result["relay_reachable"])
        relay_url = result["relay_url"]
        self.assertTrue(relay_url.startswith(TAILNET_BASE + "/r/"),
                        relay_url)
        token = relay_url.rsplit("/r/", 1)[1]
        self.assertTrue(displayd.RELOAD_TOKEN_RE.match(token))
        self.assertEqual(result["commit_url"], COMMIT_URL)
        self.assertEqual(result["params"],
                         {"sha": DEPLOYED_SHA, "relay_url": relay_url})
        # The pending-confirmation fragment shows in /state.
        state = daemon.state()
        self.assertEqual(state["renderer"], "reload")
        self.assertTrue(state["reload_confirm"]["pending"])
        self.assertGreater(state["reload_confirm"]["expires_in"], 50)

    def test_tap_only_fallback_on_loopback_bind(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        with bind_env("127.0.0.1"):
            result = daemon.reload(DEPLOYED_SHA, duration=60)
        self.assertFalse(result["relay_reachable"])
        self.assertIsNone(result["relay_url"])
        self.assertIn("tap-only", result["relay_note"])
        # Backward compatible: params stay exactly {sha}, the renderer
        # draws the commit QR plus an explicit tap note.
        self.assertEqual(result["params"], {"sha": DEPLOYED_SHA})
        self.assertEqual(result["commit_url"], COMMIT_URL)
        state = daemon.state()
        self.assertTrue(state["reload_confirm"]["pending"])
        self.assertTrue(state["reload_confirm"].get("tap_only"))

    def test_renderer_encodes_relay_shape_only(self):
        relay = TAILNET_BASE + "/r/" + "aB1-_" * 5
        payload, via = reload_view.qr_payload(DEPLOYED_SHA, relay)
        self.assertEqual((payload, via), (relay, True))
        # Anything else -- smuggled URLs, junk -- falls back to commit.
        for bad in (None, "", "https://evil.example/x",
                    "https://github.com/trillium/displayd",
                    COMMIT_URL, "/r/short", "not a url"):
            payload, via = reload_view.qr_payload(DEPLOYED_SHA, bad)
            self.assertEqual((payload, via), (COMMIT_URL, False),
                             "relay_url %r" % (bad,))

    def test_renderer_hint_names_the_confirm_path(self):
        relay = TAILNET_BASE + "/r/" + "aB1-_" * 5
        drawn = drawn_strings({"sha": DEPLOYED_SHA, "relay_url": relay})
        self.assertTrue(any("scan the code" in s for s in drawn), drawn)
        drawn = drawn_strings({"sha": DEPLOYED_SHA})
        self.assertTrue(any("tap-only" in s or "tap the panel" in s
                            for s in drawn), drawn)


@unittest.skipUnless(decoder_available(),
                     "no independent QR decoder installed (cv2/pyzbar)")
class TestReloadRelayQrPayload(unittest.TestCase):
    def test_qr_decodes_to_relay_url(self):
        relay = TAILNET_BASE + "/r/" + "aB1-_" * 5
        img = run_reload({"sha": DEPLOYED_SHA, "relay_url": relay})
        self.assertEqual(img.size, (1920, 1080))
        self.assertEqual(decode_png(to_png(img)), relay)

    def test_qr_ignores_smuggled_url_for_relay(self):
        img = run_reload({"sha": DEPLOYED_SHA,
                          "relay_url": "https://evil.example/x"})
        self.assertEqual(decode_png(to_png(img)), COMMIT_URL)


class TestRelayScanConfirm(ReloadDaemonTestCase):
    def _tailnet_reload(self, daemon, sha=DEPLOYED_SHA, duration=60):
        daemon.show("solid", {"color": "blue"})
        with bind_env(TAILNET_HOST):
            result = daemon.reload(sha, duration=duration)
        return result["relay_url"].rsplit("/r/", 1)[1]

    def test_scan_redirects_and_returns_panel(self):
        daemon = self.make_daemon()
        token = self._tailnet_reload(daemon)
        status, payload = daemon.handle_relay_scan(token)
        self.assertEqual(status, 302)
        self.assertEqual(payload["commit_url"], COMMIT_URL)
        self.assertTrue(payload["confirmed"])
        self.assertEqual(daemon.current, "solid")
        self.assertIsNone(daemon.state()["reload_confirm"])

    def test_scan_is_single_use(self):
        daemon = self.make_daemon()
        token = self._tailnet_reload(daemon)
        status, _ = daemon.handle_relay_scan(token)
        self.assertEqual(status, 302)
        # Second scan of the same token: gone, no redirect.
        status, payload = daemon.handle_relay_scan(token)
        self.assertEqual(status, 410)
        self.assertNotIn("commit_url", payload)

    def test_scan_unknown_or_malformed_token(self):
        daemon = self.make_daemon()
        self._tailnet_reload(daemon)
        for bad, want in (("0" * 22, 410),          # well-shaped, unknown
                           ("not a token!!", 404),     # malformed
                           ("", 404), (None, 404)):
            status, payload = daemon.handle_relay_scan(bad)
            self.assertEqual(status, want, "token %r" % (bad,))
            self.assertNotIn("commit_url", payload)
        # The live token still works afterwards.
        self.assertEqual(daemon.current, "reload")

    def test_stale_token_after_second_reload(self):
        daemon = self.make_daemon()
        first = self._tailnet_reload(daemon)
        with bind_env(TAILNET_HOST):
            second_url = daemon.reload(OTHER_SHA,
                                       duration=60)["relay_url"]
        second = second_url.rsplit("/r/", 1)[1]
        self.assertNotEqual(first, second)
        status, payload = daemon.handle_relay_scan(first)
        self.assertEqual(status, 410)
        self.assertNotIn("commit_url", payload)
        self.assertEqual(daemon.current, "reload")  # undisturbed

    def test_manual_show_kills_the_window(self):
        daemon = self.make_daemon()
        token = self._tailnet_reload(daemon)
        daemon.show("text", {"text": "captain takes over"})
        status, _ = daemon.handle_relay_scan(token)
        self.assertEqual(status, 410)
        result = daemon.confirm_reload("tap")
        self.assertFalse(result["confirmed"])
        self.assertEqual(daemon.current, "text")


class TestTapConfirm(ReloadDaemonTestCase):
    def test_tap_returns_panel_and_kills_token(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        with bind_env(TAILNET_HOST):
            token = daemon.reload(DEPLOYED_SHA,
                                  duration=60)["relay_url"].rsplit("/r/", 1)[1]
        result = daemon.confirm_reload("tap")
        self.assertEqual(result, {"confirmed": True, "via": "tap"})
        self.assertEqual(daemon.current, "solid")
        # The tap consumed the window: a later scan finds nothing.
        status, payload = daemon.handle_relay_scan(token)
        self.assertEqual(status, 410)
        self.assertNotIn("commit_url", payload)

    def test_tap_falls_back_path_also_confirms(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        with bind_env("127.0.0.1"):
            daemon.reload(DEPLOYED_SHA, duration=60)
        result = daemon.confirm_reload("tap")
        self.assertTrue(result["confirmed"])
        self.assertEqual(daemon.current, "solid")

    def test_tap_with_no_reload_is_a_miss(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        result = daemon.confirm_reload("tap")
        self.assertEqual(result, {"confirmed": False,
                                 "reason": "no-reload-active"})
        self.assertEqual(daemon.current, "solid")

    def test_second_tap_is_a_miss(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        with bind_env(TAILNET_HOST):
            daemon.reload(DEPLOYED_SHA, duration=60)
        self.assertTrue(daemon.confirm_reload("tap")["confirmed"])
        again = daemon.confirm_reload("tap")
        self.assertFalse(again["confirmed"])
        self.assertEqual(daemon.current, "solid")

    def test_tap_without_base_view_returns_to_clock(self):
        daemon = self.make_daemon()
        with bind_env(TAILNET_HOST):
            daemon.reload(DEPLOYED_SHA, duration=60)
        result = daemon.confirm_reload("tap")
        self.assertTrue(result["confirmed"])
        self.assertEqual(daemon.current, "clock")


class TestReloadTimeoutWithoutConfirm(ReloadDaemonTestCase):
    def test_view_returns_and_token_dies(self):
        daemon = self.make_daemon(clock=None)  # real clock: timers fire
        daemon.show("solid", {"color": "blue"})
        with bind_env(TAILNET_HOST):
            token = daemon.reload(DEPLOYED_SHA,
                                  duration=1)["relay_url"].rsplit("/r/", 1)[1]
        self.assertEqual(daemon.current, "reload")
        time.sleep(1.6)  # duration elapses with no scan and no tap
        self.assertEqual(daemon.current, "solid")
        self.assertIsNone(daemon.state()["reload_confirm"])
        status, payload = daemon.handle_relay_scan(token)
        self.assertEqual(status, 410)
        self.assertNotIn("commit_url", payload)
        # And a tap afterwards is a miss, not a view change.
        result = daemon.confirm_reload("tap")
        self.assertFalse(result["confirmed"])
        self.assertEqual(daemon.current, "solid")


class HttpRelayTestCase(HttpReloadTestCase):
    """End to end for the relay over real HTTP (302 + confirm paths)."""

    def _raw_get(self, path):
        from urllib.parse import urlsplit as _split
        parts = _split(self.base)
        conn = http.client.HTTPConnection(parts.hostname, parts.port,
                                          timeout=10)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.getheader("Location"), resp.read()
        finally:
            conn.close()

    def test_scan_relay_redirects_and_confirms(self):
        code, _ = self.call("POST", "/show",
                            {"renderer": "clock", "params": {}})
        self.assertEqual(code, 200)
        with bind_env(TAILNET_HOST):
            code, result = self.call("POST", "/reload",
                                     {"sha": DEPLOYED_SHA})
        self.assertEqual(code, 200)
        self.assertTrue(result["relay_reachable"])
        token = result["relay_url"].rsplit("/r/", 1)[1]
        status, location, _ = self._raw_get("/r/" + token)
        self.assertEqual(status, 302)
        self.assertEqual(location, COMMIT_URL)
        code, state = self.call("GET", "/state")
        self.assertEqual(code, 200)
        self.assertEqual(state["renderer"], "clock")
        self.assertIsNone(state["reload_confirm"])
        # Single-scan: the second fetch is gone.
        status, _, _ = self._raw_get("/r/" + token)
        self.assertEqual(status, 410)

    def test_tap_confirm_endpoint_returns_early(self):
        code, _ = self.call("POST", "/show",
                            {"renderer": "clock", "params": {}})
        self.assertEqual(code, 200)
        with bind_env(TAILNET_HOST):
            code, result = self.call("POST", "/reload",
                                     {"sha": DEPLOYED_SHA})
        self.assertEqual(code, 200)
        token = result["relay_url"].rsplit("/r/", 1)[1]
        code, result = self.call("POST", "/reload/confirm",
                                 {"via": "tap"})
        self.assertEqual(code, 200)
        self.assertEqual(result, {"confirmed": True, "via": "tap"})
        code, state = self.call("GET", "/state")
        self.assertEqual(state["renderer"], "clock")
        status, _, _ = self._raw_get("/r/" + token)
        self.assertEqual(status, 410)

    def test_confirm_with_no_reload_is_409(self):
        code, _ = self.call("POST", "/show",
                            {"renderer": "clock", "params": {}})
        self.assertEqual(code, 200)
        code, result = self.call("POST", "/reload/confirm",
                                 {"via": "tap"})
        self.assertEqual(code, 409)
        self.assertFalse(result["confirmed"])

    def test_tap_only_reload_marks_fallback(self):
        with bind_env("127.0.0.1"):
            code, result = self.call("POST", "/reload",
                                     {"sha": DEPLOYED_SHA})
        self.assertEqual(code, 200)
        self.assertFalse(result["relay_reachable"])
        self.assertIsNone(result["relay_url"])
        self.assertIn("tap-only", result["relay_note"])


if __name__ == "__main__":
    unittest.main()
