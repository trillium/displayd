"""Reload-confirmation tests: renderer, transient policy, HTTP endpoint.

The reload screen must show RELOADED plus the full deployed SHA, and its
QR code must decode to exactly the commit page
https://github.com/trillium/displayd/commit/<full-sha> -- never a
caller-supplied URL. POST /reload is a transient like POST /notify: the
prior view returns automatically and a manual /show wins over a late
timer. Missing, malformed, or excessive SHA/duration input is a clear 400.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

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
    def test_reload_shows_and_returns(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        result = daemon.reload(DEPLOYED_SHA, duration=60)
        self.assertEqual(daemon.current, "reload")
        self.assertEqual(result["view"], "reload")
        self.assertEqual(result["params"], {"sha": DEPLOYED_SHA})
        self.assertEqual(result["commit_url"], COMMIT_URL)
        self.assertEqual(result["return_in"], 60)
        self.assertEqual(daemon.policy.transient_status()["active"], "reload")
        token = daemon.policy.active["token"]
        daemon._transient_expired("reload", token)
        self.assertEqual(daemon.current, "solid")

    def test_reload_returns_automatically(self):
        daemon = self.make_daemon(clock=None)  # real clock: timers fire
        daemon.show("solid", {"color": "blue"})
        time.sleep(0.1)
        daemon.reload(DEPLOYED_SHA, duration=1)
        self.assertEqual(daemon.current, "reload")
        time.sleep(1.6)  # duration elapses: automatic return
        self.assertEqual(daemon.current, "solid")

    def test_default_duration_is_short(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        result = daemon.reload(DEPLOYED_SHA)
        self.assertEqual(result["return_in"], displayd.RELOAD_DEFAULT_DURATION)
        self.assertLessEqual(displayd.RELOAD_DEFAULT_DURATION, 30)

    def test_manual_change_during_reload_wins(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        daemon.reload(DEPLOYED_SHA, duration=60)
        token = daemon.policy.active["token"]
        daemon.show("text", {"text": "captain takes over"})
        self.assertEqual(daemon.current, "text")
        # The stale return timer fires late: it must not clobber the manual choice.
        daemon._transient_expired("reload", token)
        self.assertEqual(daemon.current, "text")

    def test_second_reload_replaces_first(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        daemon.reload(DEPLOYED_SHA, duration=60)
        first_token = daemon.policy.active["token"]
        daemon.reload(OTHER_SHA, duration=60)
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
        code, state = self.call("GET", "/state")
        self.assertEqual(code, 200)
        self.assertEqual(state["renderer"], "reload")
        self.assertTrue(self._wait_for_frame(), "reload drew no frame")
        code, snap = self.call("GET", "/snapshot")
        self.assertEqual(code, 200)
        if decoder_available():
            self.assertEqual(decode_png(snap), COMMIT_URL)

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


if __name__ == "__main__":
    unittest.main()
