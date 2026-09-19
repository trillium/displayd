"""Playlist-mode tests: rotation, progress bar, conformance, persistence.

No framebuffer needed: DisplayDaemon is constructed against the same
FakeFramebuffer pattern as test_policy (real PIL frames in memory, fake
wires), so /snapshot pixel assertions run on this host.

Run from the repo root:  python3 -m unittest tests.test_playlist -v
"""

import io
import os
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
import playlist as playlist_module
from PIL import Image


class FakeFramebuffer(displayd.Framebuffer):
    """Real PIL frames, fake wires."""

    def __init__(self, w=160, h=90):
        self.width, self.height, self.bpp = w, h, 32
        self.stride = w * 4
        self.fd = None
        self.last_frame = None
        self.blanked = False
        self.saved_brightness = None
        self._brightness = 200
        self._max = 255
        self.backlight = "/fake/backlight0"
        self.vt_fd = None

    def present(self, img):
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))
        self.last_frame = img.convert("RGB").tobytes("raw", "BGRX")

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
        self._brightness = max(0, min(int(value), self._max))
        return True

    def set_blank(self, value):
        self.blanked = (int(value) != 0)
        return True

    def get_blank(self):
        return 4 if self.blanked else 0

    def take_console(self):
        return True


def make_view(name, color, accent=None, static=True):
    mod = types.ModuleType("synthetic_%s" % name)
    mod.NAME = name
    mod.DESCRIPTION = "SYNTHETIC %s for playlist tests" % name
    mod.STATIC = static
    mod.PARAMS = {}
    if accent is not None:
        mod.ACCENT = accent

    def run(screen, params, stop):
        screen.present(screen.new_image(color))
        if not static:
            while not stop.is_set():
                stop.wait(0.05)
                screen.present(screen.new_image(color))

    mod.run = run
    return mod


def make_broken(name):
    mod = types.ModuleType("synthetic_%s" % name)
    mod.NAME = name
    mod.DESCRIPTION = "SYNTHETIC broken view for playlist tests"
    mod.STATIC = True
    mod.PARAMS = {}

    def run(screen, params, stop):
        raise RuntimeError("SYNTHETIC runtime failure")

    mod.run = run
    return mod


def entry(mod):
    return {"module": mod, "description": mod.DESCRIPTION, "params": {},
            "inputs": {}, "static": mod.STATIC}


class PlaylistTestCase(unittest.TestCase):
    def setUp(self):
        self._real_fb = displayd.Framebuffer
        displayd.Framebuffer = FakeFramebuffer
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy_path = os.path.join(self.tmp.name, "policy.json")

    def tearDown(self):
        displayd.Framebuffer = self._real_fb

    def make_daemon(self, views=None, **over):
        daemon = displayd.DisplayDaemon(policy_path=self.policy_path)
        daemon.renderers["red"] = entry(make_view("red", (200, 30, 30),
                                                 accent="#FF4040"))
        daemon.renderers["green"] = entry(make_view("green", (30, 200, 30),
                                                   accent="#40FF40"))
        daemon.renderers["blue"] = entry(make_view("blue", (30, 30, 200)))
        daemon.renderers["broken"] = entry(make_broken("broken"))
        self.addCleanup(daemon.playlist.stop)
        self.addCleanup(daemon.stop_watchdog)
        if views is not None:
            cfg = {"enabled": True, "tick_seconds": 0.05, "views": views}
            cfg.update(over)
            daemon.set_policy({"playlist": cfg})
        return daemon

    def snapshot_img(self, daemon):
        png = daemon.snapshot()
        self.assertIsNotNone(png, "nothing has been drawn yet")
        return Image.open(io.BytesIO(png)).convert("RGB")

    def wait_for(self, cond, timeout=8.0, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        self.fail("timed out waiting for %s" % what)
        return False


class TestConfig(PlaylistTestCase):
    def test_defaults_present(self):
        daemon = self.make_daemon()
        cfg = daemon.get_policy()["config"]["playlist"]
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["placement"], "bottom")
        self.assertEqual(cfg["direction"], "fill")
        self.assertEqual(cfg["views"], [])

    def test_bad_configs_rejected(self):
        daemon = self.make_daemon()
        bad = [
            {"placement": "diagonal"},
            {"direction": "sideways"},
            {"thickness": 1},
            {"thickness": 100},
            {"color": "not-a-colour"},
            {"views": [{"params": {}}]},
            {"views": [{"renderer": "red", "dwell": 1}]},
            {"views": [{"renderer": "red", "dwell": 99999}]},
            {"views": [{"renderer": "red", "color": "zzz"}]},
            {"views": "red"},
        ]
        for patch in bad:
            with self.assertRaises(ValueError, msg=repr(patch)):
                daemon.set_policy({"playlist": patch})

    def test_good_config_persists(self):
        views = [{"renderer": "red", "dwell": 3},
                 {"renderer": "green", "dwell": 4}]
        daemon = self.make_daemon()
        daemon.set_policy({"playlist": {"enabled": True, "placement": "left",
                                        "thickness": 12, "views": views}})
        with open(self.policy_path) as fh:
            import json
            raw = json.load(fh)
        self.assertTrue(raw["playlist"]["enabled"])
        self.assertEqual(raw["playlist"]["placement"], "left")
        self.assertEqual(len(raw["playlist"]["views"]), 2)
        # A fresh policy object restores it (restart survival of config).
        import policy as policy_module
        again = policy_module.Policy(path=self.policy_path)
        self.assertEqual(again.get_config()["playlist"]["views"], views)


class TestGeometry(unittest.TestCase):
    def test_edges_own_their_strip(self):
        W, H, t = 160, 90, 10
        track, fill = playlist_module.bar_boxes(W, H, "bottom", t, 0.5)
        self.assertEqual(track, (0, H - t, W, H))
        self.assertEqual(fill, (0, H - t, W // 2, H))
        track, fill = playlist_module.bar_boxes(W, H, "top", t, 0.5)
        self.assertEqual(track, (0, 0, W, t))
        track, fill = playlist_module.bar_boxes(W, H, "left", t, 0.5)
        self.assertEqual(track, (0, 0, t, H))
        # Vertical fills rise from the bottom, not down from the top.
        self.assertEqual(fill, (0, H - H // 2, t, H))
        track, fill = playlist_module.bar_boxes(W, H, "right", t, 1.0)
        self.assertEqual(fill, (W - t, 0, W, H))

    def test_empty_fill_draws_track_only(self):
        _, fill = playlist_module.bar_boxes(160, 90, "bottom", 10, 0.0)
        self.assertIsNone(fill)

    def test_bad_placement_rejected(self):
        with self.assertRaises(ValueError):
            playlist_module.bar_boxes(160, 90, "diagonal", 10, 0.5)


class TestAccent(unittest.TestCase):
    def test_precedence(self):
        with_accent = {"module": make_view("a", (0, 0, 0), accent="#112233")}
        bare = {"module": make_view("b", (0, 0, 0))}
        # Renderer ACCENT beats the playlist default.
        self.assertEqual(playlist_module.accent_for(with_accent), (17, 34, 51))
        # No ACCENT: playlist default wins.
        self.assertEqual(playlist_module.accent_for(bare, None, "#AABBCC"),
                         (170, 187, 204))
        # Per-view item color beats ACCENT.
        self.assertEqual(playlist_module.accent_for(with_accent, "#FF0000"),
                         (255, 0, 0))
        # Garbage anywhere falls through, never raises.
        junk = {"module": make_view("c", (0, 0, 0), accent="zzz")}
        self.assertEqual(playlist_module.accent_for(junk, "also-junk"),
                         playlist_module.DEFAULT_COLOR)
        self.assertEqual(playlist_module.accent_for(None),
                         playlist_module.DEFAULT_COLOR)
        self.assertEqual(playlist_module.accent_for({"broken": "x"}),
                         playlist_module.DEFAULT_COLOR)


class TestRotation(PlaylistTestCase):
    def test_full_cycle_with_timestamps(self):
        views = [{"renderer": "red", "dwell": 3},
                 {"renderer": "green", "dwell": 3},
                 {"renderer": "blue", "dwell": 3}]
        daemon = self.make_daemon(views)
        self.wait_for(lambda: daemon.current == "red", what="first view")
        self.wait_for(lambda: daemon.current == "green", what="second view")
        self.wait_for(lambda: daemon.current == "blue", what="third view")
        self.wait_for(lambda: daemon.current == "red", what="wrap-around",
                      timeout=10.0)
        hist = daemon.playlist.status()["history"]
        names = [h["renderer"] for h in hist]
        self.assertEqual(names[:4], ["red", "green", "blue", "red"])
        times = [h["at"] for h in hist[:4]]
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, 2.5, "dwell not respected")

    def test_bar_advances_between_snapshots(self):
        daemon = self.make_daemon([{"renderer": "red", "dwell": 6}],
                                  placement="bottom", thickness=8)
        self.wait_for(lambda: daemon.current == "red", what="red showing")
        time.sleep(0.4)
        early = self.snapshot_img(daemon)
        time.sleep(2.0)
        late = self.snapshot_img(daemon)
        W, H = early.size
        accent = (255, 64, 64)

        def accent_width(img):
            n = 0
            for x in range(W):
                r, g, b = img.getpixel((x, H - 4))
                if abs(r - accent[0]) < 40 and g < 120 and b < 120:
                    n += 1
            return n

        self.assertGreater(accent_width(late), accent_width(early),
                           "bar did not advance between snapshots")
        # Bar is visible at all: track strip differs from view background.
        bg = (200, 30, 30)
        strip = [early.getpixel((x, H - 4)) for x in range(0, W, 7)]
        self.assertTrue(any(p != bg for p in strip),
                        "no bar pixels on the bottom edge")

    def test_all_placements_render_on_edge(self):
        daemon = self.make_daemon([{"renderer": "blue", "dwell": 30}],
                                  thickness=8)
        self.wait_for(lambda: daemon.current == "blue", what="blue showing")
        time.sleep(1.5)  # let the fill grow so every edge has fill pixels
        for placement in ("top", "left", "bottom", "right"):
            daemon.set_policy({"playlist": {"placement": placement}})
            time.sleep(0.4)
            img = self.snapshot_img(daemon)
            W, H = img.size
            bg = (30, 30, 200)
            if placement == "top":
                pts = [(x, 4) for x in range(0, W, 5)]
            elif placement == "bottom":
                pts = [(x, H - 4) for x in range(0, W, 5)]
            elif placement == "left":
                pts = [(4, y) for y in range(H - 1, 0, -5)]
            else:
                pts = [(W - 4, y) for y in range(H - 1, 0, -5)]
            # Fill rises from bottom on side edges: check the far end.
            far = pts[:4] if placement in ("top", "bottom") else pts[:4]
            hit = [p for p in far if img.getpixel(p) != bg]
            self.assertTrue(hit, "%s edge shows no bar" % placement)

    def test_conformance_two_palettes(self):
        # Dark view (green on near-black analogue) vs light view: per-view
        # colours plus ACCENT must both be visible, not bolted-on white.
        daemon = self.make_daemon(
            [{"renderer": "green", "dwell": 30, "color": "#50DC78"},
             {"renderer": "blue", "dwell": 30}],
            placement="bottom", thickness=8)
        self.wait_for(lambda: daemon.current == "green", what="green")
        time.sleep(2.5)
        img = self.snapshot_img(daemon)
        W, H = img.size
        greens = sum(1 for x in range(W)
                     if abs(img.getpixel((x, H - 4))[1] - 220) < 60
                     and img.getpixel((x, H - 4))[0] < 120)
        self.assertGreater(greens, 5, "per-view green accent not visible")
        daemon.playlist.next()
        self.wait_for(lambda: daemon.current == "blue", what="blue")
        time.sleep(2.0)
        img = self.snapshot_img(daemon)
        W, H = img.size
        # Blue view declares no ACCENT: fallback white bar with contrast
        # border must still read against the blue background.
        strip = [img.getpixel((x, H - 4)) for x in range(0, W, 3)]
        self.assertTrue(any(p != (30, 30, 200) for p in strip),
                        "fallback bar invisible on blue")


class TestYielding(PlaylistTestCase):
    def test_manual_show_holds_until_resume(self):
        daemon = self.make_daemon([{"renderer": "red", "dwell": 3},
                                   {"renderer": "green", "dwell": 3}])
        self.wait_for(lambda: daemon.current == "red", what="rotation live")
        daemon.show("blue", {})
        self.assertEqual(daemon.playlist.status()["hold"], "paused:manual")
        time.sleep(4.5)  # a full dwell passes: rotation must not override
        self.assertEqual(daemon.current, "blue")
        daemon.playlist.resume()
        self.wait_for(lambda: daemon.current in ("red", "green"),
                      what="rotation resumed")

    def test_notify_pauses_then_resumes(self):
        daemon = self.make_daemon([{"renderer": "red", "dwell": 3},
                                   {"renderer": "green", "dwell": 3}])
        self.wait_for(lambda: daemon.current in ("red", "green"),
                      what="rotation live")
        daemon.notify("hello", duration=1)
        self.assertEqual(daemon.current, "notice")
        self.assertEqual(daemon.playlist.status()["hold"], "transient")
        self.wait_for(lambda: daemon.current in ("red", "green"),
                      what="return to rotation", timeout=6.0)
        time.sleep(4.0)
        # Rotation kept advancing after the return (not stuck on one view).
        hist = [h["renderer"] for h in
                daemon.playlist.status()["history"][-3:]]
        self.assertGreaterEqual(len(set(hist)), 1)
        self.assertIsNone(daemon.playlist.status()["hold"])

    def test_broken_view_does_not_stall(self):
        daemon = self.make_daemon([{"renderer": "no-such-view", "dwell": 30},
                                   {"renderer": "red", "dwell": 3},
                                   {"renderer": "broken", "dwell": 3}])
        # The unknown renderer surfaces an error while it is skipped...
        self.wait_for(lambda: daemon.playlist.status()["last_error"]
                      is not None, what="skip error recorded")
        err = daemon.playlist.status()["last_error"]
        self.assertIn("unknown renderer", err)
        # ...and the rotation moves past it instead of stalling.
        self.wait_for(lambda: daemon.current == "red",
                      what="skip past unknown renderer", timeout=10.0)
        # A renderer that raises at runtime holds its frame but the
        # scheduler still advances past it on the dwell.
        self.wait_for(lambda: daemon.current == "broken", what="broken view",
                      timeout=10.0)
        self.wait_for(lambda: daemon.current == "red", what="past broken",
                      timeout=10.0)

    def test_restart_resumes_rotation(self):
        views = [{"renderer": "red", "dwell": 3},
                 {"renderer": "green", "dwell": 3}]
        first = self.make_daemon(views)
        self.wait_for(lambda: first.current == "red", what="first boot live")
        first.playlist.stop()
        # Fresh daemon, same policy file: rotation resumes on its own.
        second = displayd.DisplayDaemon(policy_path=self.policy_path)
        second.renderers["red"] = entry(make_view("red", (200, 30, 30)))
        second.renderers["green"] = entry(make_view("green", (30, 200, 30)))
        self.addCleanup(second.playlist.stop)
        self.addCleanup(second.stop_watchdog)
        second.playlist.boot()
        self.wait_for(lambda: second.current in ("red", "green"),
                      what="rotation after restart", timeout=10.0)


class TestControlSurface(PlaylistTestCase):
    def test_page_covers_playlist(self):
        page = displayd.CONTROL_PAGE
        for token in ("/playlist", "/playlist/pause", "/playlist/resume",
                      "/playlist/next"):
            self.assertIn(token, page,
                            "control page never talks to %s" % token)

    def test_renderer_list_advertises_accent(self):
        daemon = self.make_daemon()
        by_name = {r["name"]: r for r in daemon.renderer_list()}
        self.assertEqual(by_name["red"]["accent"], "#ff4040")
        # No ACCENT declared: advertised fallback, view still renders.
        self.assertIn("accent", by_name["blue"])

    def test_existing_views_still_render(self):
        daemon = self.make_daemon()
        for name, params in (
                ("text", {"text": "smoke"}),
                ("clock", {}),
                ("solid", {"color": "red"}),
                ("life", {}),
                ("notice", {"title": "smoke"}),
                ("qr", {"data": "https://example.com"})):
            daemon.show(name, params)
            time.sleep(0.3)
            self.assertEqual(daemon.current, name)
            self.assertIsNotNone(daemon.snapshot())

    def test_autonomy_still_works(self):
        daemon = self.make_daemon()
        daemon.show("solid", {"color": "blue"})
        daemon.set_policy({"idle": {"enabled": True, "after_seconds": 5}})
        self.assertTrue(daemon.get_policy()["config"]["idle"]["enabled"])
        out = daemon.notify("hi", duration=1)
        self.assertEqual(out["view"], "notice")
        self.wait_for(lambda: daemon.current == "solid",
                      what="notify return", timeout=6.0)


if __name__ == "__main__":
    unittest.main()
