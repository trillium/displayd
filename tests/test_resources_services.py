"""Tests for the resources and services wall views. No framebuffer needed:
nothing here instantiates DisplayDaemon or touches /dev/fb0.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "renderers"))

import displayd

from PIL import Image

import resources
import services


class FakeScreen:
    W, H = 1920, 1080

    def __init__(self):
        self.frames = []

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        return displayd.Screen.color(value, default)

    @staticmethod
    def font_path(family="DejaVuSans-Bold"):
        return None  # CI box has no fonts: renderers must survive that


def _nonblank(img):
    bg = img.getpixel((0, 0))
    for x, y in ((10, 10), (960, 540), (1900, 1060), (300, 300), (1500, 800)):
        if img.getpixel((x, y)) != bg:
            return True
    # Fall back to a full scan before calling it blank.
    extrema = img.getextrema()
    return any(lo != hi for lo, hi in extrema)


class TestContracts(unittest.TestCase):
    def test_both_renderers_load(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        for name in ("resources", "services"):
            self.assertIn(name, found, "renderer %r missing" % name)
            self.assertIn("module", found[name], "%r failed to import: %s"
                          % (name, found[name].get("broken")))

    def test_param_schemas_are_well_formed(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        for name in ("resources", "services"):
            params = found[name]["params"]
            self.assertIsInstance(params, dict)
            for pname, spec in params.items():
                self.assertIsInstance(spec, dict)
                self.assertIn(spec.get("type"), {"string", "integer", "number", "boolean"})

    def test_not_static(self):
        self.assertFalse(resources.STATIC)
        self.assertFalse(services.STATIC)


class TestCpuDelta(unittest.TestCase):
    def test_first_sample_has_no_delta(self):
        self.assertIsNone(resources._cpu_pct(None, (1000, 800)))

    def test_idle_box_reads_near_zero(self):
        prev = (1000000, 950000)
        cur = (1001000, 950950)  # 950 of 1000 new ticks idle
        self.assertAlmostEqual(resources._cpu_pct(prev, cur), 5.0, places=6)

    def test_busy_box_reads_high(self):
        prev = (1000000, 200000)
        cur = (1001000, 200050)  # 950 of 1000 new ticks busy
        self.assertAlmostEqual(resources._cpu_pct(prev, cur), 95.0, places=6)

    def test_saturated_host_still_reads_sane(self):
        # Load-300 style box: counters huge, tiny idle delta, must clamp 0..100.
        prev = (23333976 + 1013280 + 205501, 23333976)
        cur = (prev[0] + 4000, prev[1] + 12)
        pct = resources._cpu_pct(prev, cur)
        self.assertGreaterEqual(pct, 0.0)
        self.assertLessEqual(pct, 100.0)
        self.assertAlmostEqual(pct, 99.7, places=1)

    def test_stalled_counters_give_none_not_nonsense(self):
        self.assertIsNone(resources._cpu_pct((5000, 4000), (5000, 4000)))

    def test_parse_cpu_line(self):
        total, idle = resources._parse_cpu_line(
            "cpu  1013280 597 205501 23333976 40055 0 15693 0 0 0\n"
            "cpu0 240127 143 51476 5849650 10481 0 1220 0 0 0\n")
        self.assertEqual(idle, 23333976 + 40055)
        self.assertEqual(total, 1013280 + 597 + 205501 + 23333976 + 40055 + 0 + 15693)

    def test_parse_cpu_line_missing_raises(self):
        with self.assertRaises(ValueError):
            resources._parse_cpu_line("cpu0 1 2 3 4\n")

    @unittest.skipUnless(os.path.exists("/proc/stat"), "needs Linux /proc")
    def test_two_real_reads_form_a_delta(self):
        with open("/proc/stat") as fh:
            first = fh.read()
        import time as _t
        _t.sleep(0.2)
        with open("/proc/stat") as fh:
            second = fh.read()
        pct = resources._cpu_pct(resources._parse_cpu_line(first),
                                 resources._parse_cpu_line(second))
        self.assertIsNotNone(pct)
        self.assertGreaterEqual(pct, 0.0)
        self.assertLessEqual(pct, 100.0)


class TestProcParsing(unittest.TestCase):
    def test_loadavg(self):
        l1, l5, l15, procs = resources._parse_loadavg("0.00 0.03 0.08 1/446 235572\n")
        self.assertEqual((l1, l5, l15, procs), (0.0, 0.03, 0.08, "1/446"))

    def test_meminfo_with_available(self):
        mem = resources._parse_meminfo(
            "MemTotal:        15904 kB\nMemAvailable:  14442 kB\n"
            "SwapTotal:        4095 kB\nSwapFree:       4095 kB\n")
        self.assertEqual(mem["total"], 15904 * 1024)
        self.assertEqual(mem["used"], (15904 - 14442) * 1024)
        self.assertEqual(mem["swap_used"], 0)

    def test_meminfo_fallback_without_available(self):
        mem = resources._parse_meminfo(
            "MemTotal:        1000 kB\nMemFree:          100 kB\n"
            "Buffers:          100 kB\nCached:           300 kB\n")
        self.assertEqual(mem["used"], 500 * 1024)

    def test_meminfo_missing_total_raises(self):
        with self.assertRaises(ValueError):
            resources._parse_meminfo("MemFree: 1 kB\n")

    @unittest.skipUnless(os.path.exists("/proc/loadavg"), "needs Linux /proc")
    def test_live_proc_files_parse(self):
        with open("/proc/loadavg") as fh:
            resources._parse_loadavg(fh.read())
        with open("/proc/meminfo") as fh:
            mem = resources._parse_meminfo(fh.read())
        self.assertGreater(mem["total"], 0)


class TestServicesSnapshot(unittest.TestCase):
    def _payload(self):
        return {
            "hostname": "lnx-server",
            "host_uptime": "up 17 hours, 9 minutes",
            "generated_at": 1789839381,
            "services": [
                {"name": "displayd.service", "state": "up", "detail": "active/running",
                 "uptime": "today", "restarts": "0"},
                {"name": "docker.service", "state": "up", "detail": "active/running",
                 "uptime": "today", "restarts": "1"},
                {"name": "cron.service", "state": "down", "detail": "inactive/dead",
                 "uptime": "", "restarts": "0"},
                {"name": "snap.firmware-updater.firmware-notifier.service",
                 "state": "failed", "detail": "failed/failed", "uptime": "",
                 "restarts": "0"},
            ],
            "containers": [
                {"name": "coder", "state": "running", "status": "Up 17 hours"},
                {"name": "coder-admin-proof1", "state": "exited", "status": "Exited"},
            ],
            "listeners": [
                {"address": "0.0.0.0", "port": 22, "process": "sshd"},
                {"address": "::", "port": 22, "process": "sshd"},
                {"address": "0.0.0.0", "port": 7080, "process": "docker-proxy"},
                {"address": "100.81.88.113", "port": 56680, "process": "tailscaled"},
            ],
        }

    def test_tallies_and_failed(self):
        snap = services._build_snapshot(self._payload(), "")
        self.assertEqual((snap["up"], snap["down"], snap["failed"]), (2, 1, 1))
        self.assertEqual(snap["failed_units"],
                         ["snap.firmware-updater.firmware-notifier.service"])

    def test_watch_spotlights_known_and_unknown(self):
        snap = services._build_snapshot(
            self._payload(), "displayd.service,bogus.service")
        self.assertEqual(snap["watched"][0]["state"], "up")
        self.assertEqual(snap["watched"][1]["state"], "unknown")

    def test_ports_deduped_and_ephemeral_dropped(self):
        snap = services._build_snapshot(self._payload(), "")
        ports = [p["port"] for p in snap["ports"]]
        self.assertIn(22, ports)
        self.assertIn(7080, ports)
        self.assertEqual(len(ports), len(set(ports)))
        self.assertNotIn(56680, ports)

    def test_priority_ports_come_first(self):
        payload = self._payload()
        payload["listeners"] = [
            {"address": "0.0.0.0", "port": 9999, "process": "zzz"},
            {"address": "0.0.0.0", "port": 8181, "process": "python3"},
            {"address": "0.0.0.0", "port": 8980, "process": "python3"},
            {"address": "0.0.0.0", "port": 1111, "process": "aaa"},
        ]
        snap = services._build_snapshot(payload, "")
        self.assertEqual([p["port"] for p in snap["ports"]],
                         [8181, 8980, 1111, 9999])

    def test_malformed_payloads_raise(self):
        for bad in (None, [], {}, {"services": "nope"}, {"services": None}):
            with self.assertRaises(ValueError):
                services._build_snapshot(bad, "")

    def test_missing_lists_default_empty(self):
        snap = services._build_snapshot({"services": []}, "")
        self.assertEqual(snap["containers"], [])
        self.assertEqual(snap["ports"], [])


class TestColdAndErrorFrames(unittest.TestCase):
    def _set(self, mod, snapshot, health, error=None):
        with mod._POLL["lock"]:
            mod._POLL["snapshot"] = snapshot
            mod._POLL["health"] = health
            mod._POLL["error"] = error
            mod._POLL["updated"] = 0.0 if snapshot is None else __import__("time").time()

    def test_resources_cold_frame(self):
        self._set(resources, None, "cold")
        screen = FakeScreen()
        resources._draw(screen, "RESOURCES", (8, 8, 12))
        self.assertTrue(screen.frames)
        self.assertEqual(screen.frames[-1].size, (1920, 1080))
        self.assertTrue(_nonblank(screen.frames[-1]))

    def test_resources_error_frame(self):
        self._set(resources, None, "error", "boom")
        screen = FakeScreen()
        resources._draw(screen, "RESOURCES", (8, 8, 12))
        self.assertTrue(_nonblank(screen.frames[-1]))

    def test_resources_populated_frame(self):
        snap = {
            "cpu_pct": 12.5, "load": (0.5, 0.4, 0.3), "procs": "2/446",
            "mem_used": 2 * 1024 ** 3, "mem_total": 16 * 1024 ** 3,
            "swap_used": 0, "swap_total": 4 * 1024 ** 3,
            "disks": [{"mount": "/", "used": 24 * 1024 ** 3,
                       "total": 98 * 1024 ** 3, "pct": 24.5}],
            "uptime_s": 61200, "hostname": "lnx-server", "ncpu": 4,
        }
        self._set(resources, snap, "warm")
        screen = FakeScreen()
        resources._draw(screen, "RESOURCES", (8, 8, 12))
        self.assertTrue(_nonblank(screen.frames[-1]))

    def test_resources_sampling_frame(self):
        snap = dict({
            "cpu_pct": None, "load": (0.0, 0.0, 0.0), "procs": "1/1",
            "mem_used": 1, "mem_total": 2, "swap_used": 0, "swap_total": 0,
            "disks": [], "uptime_s": 1, "hostname": "h", "ncpu": 1,
        })
        self._set(resources, snap, "warm")
        screen = FakeScreen()
        resources._draw(screen, "RESOURCES", (8, 8, 12))
        self.assertTrue(_nonblank(screen.frames[-1]))

    def test_services_cold_frame(self):
        self._set(services, None, "cold")
        screen = FakeScreen()
        services._draw(screen, "SERVICES", (8, 8, 12), services.DEFAULT_URL)
        self.assertTrue(_nonblank(screen.frames[-1]))

    def test_services_down_source_frame(self):
        self._set(services, None, "error", "connection refused")
        screen = FakeScreen()
        services._draw(screen, "SERVICES", (8, 8, 12), services.DEFAULT_URL)
        self.assertTrue(_nonblank(screen.frames[-1]))

    def test_services_populated_frame(self):
        snap = services._build_snapshot(
            TestServicesSnapshot()._payload(),
            "displayd.service,docker.service")
        self._set(services, snap, "warm")
        screen = FakeScreen()
        services._draw(screen, "SERVICES", (8, 8, 12), services.DEFAULT_URL)
        self.assertTrue(_nonblank(screen.frames[-1]))

    def test_run_returns_quickly_with_no_data(self):
        # A cold start must present a frame without waiting on any poll.
        for mod, kwargs in ((resources, {}), (services, {})):
            self._set(mod, None, "cold")
            screen = FakeScreen()
            stop = threading.Event()
            if mod is services:
                thread = threading.Thread(
                    target=mod.run, args=(screen, {"interval": 3600}, stop),
                    daemon=True)
            else:
                thread = threading.Thread(
                    target=mod.run,
                    args=(screen, {"interval": 3600, "mounts": "/"}, stop),
                    daemon=True)
            thread.start()
            deadline = __import__("time").time() + 10
            while not screen.frames and __import__("time").time() < deadline:
                __import__("time").sleep(0.1)
            self.assertTrue(screen.frames, "%s drew nothing" % mod.NAME)
            stop.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "%s run() hung" % mod.NAME)
            with mod._POLL["lock"]:
                mod._POLL["cfg"] = {}


if __name__ == "__main__":
    unittest.main()
