"""Fixtures and loopback probes for the tap-latency harness.

Single concept: the static material one measurement run needs -- the display
set and the macbook state document the headless daemon is fed, the Quartz
point -> panel pixel mapping, and the timed loopback GET/POST probes that
stand in for the wire between the tap source and the daemon.
"""

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "renderers"))

import macbook_layout  # noqa: E402
import macbook_map  # noqa: E402

PANEL_W, PANEL_H = 1920, 1080
DISPLAYS = [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
             "main": True},
            {"bounds": {"x": -355, "y": -1080, "w": 1920, "h": 1080},
             "main": False}]


def macbook_feed(ts=None):
    return {
        "ts": ts if ts is not None else time.time(),
        "accessibility_trusted": True,
        "focus": {"app_name": "WezTerm",
                  "window_title": "macbookpro: fm-",
                  "window_bounds": {"x": 0, "y": -1049,
                                    "w": 1920, "h": 1049},
                  "display_index": 1},
        "mouse": {"x": 464, "y": -283, "display_index": 1},
        "displays": [{"bounds": dict(d["bounds"]), "main": d["main"]}
                     for d in DISPLAYS],
        "talon": {"mode": "command", "muted": False},
    }


def http_post(base, path, body, timeout=5.0):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    return (time.perf_counter() - t0) * 1000.0, payload


def http_get(base, path, timeout=10.0):
    t0 = time.perf_counter()
    with urllib.request.urlopen(base + path,
                                timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    return (time.perf_counter() - t0) * 1000.0, payload


def panel_of(qx, qy):
    box = macbook_map.union(DISPLAYS)
    scale, ox, oy = macbook_map.frame(
        box, PANEL_W, PANEL_H,
        top=macbook_layout.header_bottom(), bottom=PANEL_H)
    px, py = macbook_map.project(qx, qy, scale, ox, oy)
    return int(round(px)), int(round(py))
