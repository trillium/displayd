"""Tests for tools/regen_touch_tiles.py (host touch.json tile regeneration).

Hermetic: a stub /renderers server on loopback, a temp touch.json.

Run from the repo root:  python3 -m unittest tests.test_regen_touch_tiles -v
"""

import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, os.pardir))
sys.path.insert(0, os.path.join(HERE, os.pardir, "tools"))

import regen_touch_tiles as rg
from renderers import picker, unified

SLEEP = {"id": "sleep-x", "rect": [0, 0, 10, 10],
         "action": {"name": "screen_on"}}
OLD = ["clock", "life", "macbook", "options"]
NEW = sorted(OLD + ["html"])


def doc(names, gated=("text",)):
    return {"renderers": [{"name": n, "params": {}} for n in names] + [
        {"name": g, "params": {"x": {"required": True}}} for g in gated
    ] + [{"name": "bad", "broken": "x", "params": {}}]}


def serve(payload):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def host_config(views):
    return {"width": 1920, "height": 1080, "regions": [],
            "view_regions": {
                "picker": picker.picker_regions(1920, 1080, views),
                "unified": unified.unified_regions(
                    1920, 1080, [v for v in views if v != "unified"])[1:],
                "sleep": [SLEEP]}}


class RegenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "touch.json")

    def run_tool(self, names, *extra):
        srv = serve(doc(names))
        self.addCleanup(srv.shutdown)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rg.main(["--config", self.path, "--endpoint",
                          "http://127.0.0.1:%d" % srv.server_port, *extra])
        return rc, out.getvalue()

    def write(self, cfg):
        with open(self.path, "w") as fh:
            json.dump(cfg, fh)

    def test_live_set_drops_gated_and_broken(self):
        self.assertEqual(rg.live_set(doc(["b", "a"])), ["a", "b"])

    def test_in_sync_exits_zero(self):
        self.write(host_config(NEW))
        rc, out = self.run_tool(NEW)
        self.assertEqual(rc, 0)
        self.assertIn("picker: in sync", out)

    def test_dry_run_reports_drift_and_writes_nothing(self):
        self.write(host_config(OLD))
        before = open(self.path).read()
        rc, out = self.run_tool(NEW)
        self.assertEqual(rc, 1)
        self.assertIn("view-html", out)
        self.assertEqual(open(self.path).read(), before)

    def test_apply_backs_up_and_regenerates(self):
        self.write(host_config(OLD))
        old_text = open(self.path).read()
        rc, out = self.run_tool(NEW, "--apply")
        self.assertEqual(rc, 0)
        cfg = json.load(open(self.path))
        self.assertEqual(cfg["view_regions"]["picker"],
                         picker.picker_regions(1920, 1080, NEW))
        self.assertIn("uview-html",
                      [e["id"] for e in cfg["view_regions"]["unified"]])
        self.assertEqual(cfg["view_regions"]["sleep"], [SLEEP])
        backups = os.path.join(self.tmp.name, "backups")
        (name,) = os.listdir(backups)
        self.assertEqual(open(os.path.join(backups, name)).read(), old_text)
        self.assertIn("revert: cp -p", out)

    def test_absent_scope_is_not_added(self):
        cfg = host_config(OLD)
        del cfg["view_regions"]["unified"]
        self.write(cfg)
        self.run_tool(NEW, "--apply")
        self.assertNotIn("unified", json.load(open(self.path))["view_regions"])


if __name__ == "__main__":
    unittest.main()
