"""Tap-to-action latency harness: measure before cutting, re-measure after.

Emulates one panel tap end to end against a headless daemon over loopback
HTTP (the HTTP/queue floor; tailnet adds ~1-5ms, negligible at this scale)
and breaks the latency down by stage:

  A  evdev edge -> dispatched action (touch resolve + POST round trip)
  B  daemon queue (POST handler incl. feed-resolution + re-show)
  C  queue -> poller fetch (bridge 0.5s tick wait; the suspected bottleneck)
  D  visible response (daemon-measured request -> first presented pixel,
     request -> freshly drawn frame; per captain's fact the draw call is
     the variable cost, everything else static/cacheable)

Usage:
  python3 tools/tap_latency.py [--taps N] [--interval S] [--wait S]
                               [--port P] [--json-out PATH]

  --wait S passes ?wait=S on the poller GETs (long-poll mode); default 0
  reproduces today's short-poll behavior.

Run from the repo root.
"""

import json
import os
import random
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "renderers"))

os.environ["DISPLAYD_FAKE_FB"] = "1"

import displayd  # noqa: E402
import macbook_layout  # noqa: E402
import macbook_map  # noqa: E402
import touch  # noqa: E402

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


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--taps", type=int, default=20)
    ap.add_argument("--interval", type=float, default=0.5,
                    help="emulated bridge poll cadence (s)")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="?wait= long-poll hold on poller GETs (s)")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    tmp = tempfile.TemporaryDirectory()
    daemon = displayd.DisplayDaemon(
        policy_path=os.path.join(tmp.name, "policy.json"),
        feedback_path=os.path.join(tmp.name, "feedback.jsonl"))
    daemon.show("macbook", {})
    daemon.feed("macbook", "state", macbook_feed())
    daemon.feed("talon_apps", "state",
                {"ts": time.time(), "apps": ["Safari", "Terminal", "Mail"],
                 "focused": "Safari"})
    displayd.DAEMON = daemon
    server = ThreadingHTTPServer(("127.0.0.1", 0), displayd.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % port

    try:
        # Stage A: resolve cost (evdev parse is a sub-ms struct.unpack;
        # the resolve + HTTP POST is the dispatch cost that matters).
        action = {"name": "macbook_mouse",
                  "x": 960, "y": 600}
        t0 = time.perf_counter()
        for _ in range(50):
            touch.resolve_action(action, panel=(PANEL_W, PANEL_H))
        resolve_ms = (time.perf_counter() - t0) * 1000.0 / 50.0

        post_ms, fetch_ms, poll_wait_ms = [], [], []
        switch_ms, frame_ms = [], []
        px, py = panel_of(100, 100)
        last_seen = 0.0
        rng = random.Random(1234)
        # Emulated bridge poller at the production 0.5s cadence; taps
        # land at random phase so the wait distribution is honest.
        stop = threading.Event()
        seen = {}

        def poller(path, key):
            # Same shape as the production bridge loop: hold the GET
            # (long-poll when --wait is set), then sleep only the
            # remainder of the tick -- except right after a command was
            # acted on, when the next hold re-parks at once (0.05 floor)
            # so a back-to-back tap wakes instead of riding the sleep.
            since = [0.0]
            while not stop.is_set():
                t0 = time.monotonic()
                acted = False
                try:
                    _, doc = http_get(
                        base, "%s?since=%s%s" % (
                            path, since[0],
                            ("&wait=%s" % args.wait) if args.wait else ""),
                        timeout=10.0)
                    cmd = (doc.get("command") or {})
                    if isinstance(cmd, dict) and cmd.get("ts", 0) > 0:
                        since[0] = max(since[0], float(cmd["ts"]))
                        seen[key] = (time.perf_counter(), dict(cmd))
                        acted = True
                except Exception:
                    pass
                if acted:
                    time.sleep(0.05)
                else:
                    time.sleep(max(0.05, args.interval
                                   - (time.monotonic() - t0)))

        pt = threading.Thread(target=poller,
                              args=("/macbook/mouse", "mouse"),
                              daemon=True)
        pt.start()
        # Refresh feeds each tap (production bridges POST state ~2Hz;
        # without this the freshness gates would refuse).
        for i in range(args.taps):
            # Each tap re-enters from GLANCE: a queued mouse tap pins
            # the showing view to AIM, where the next map tap refuses.
            daemon.show("macbook", {})
            daemon.feed("macbook", "state", macbook_feed())
            daemon.feed("talon_apps", "state",
                        {"ts": time.time(),
                         "apps": ["Safari", "Terminal", "Mail"],
                         "focused": "Safari"})
            seen.pop("mouse", None)
            t_post = time.perf_counter()
            ms, doc = http_post(base, "/macbook/mouse",
                                {"x": px, "y": py})
            assert doc.get("ok"), doc
            t_queued = time.perf_counter()
            post_ms.append(ms)
            deadline = t_queued + max(5.0, args.interval * 3 + 2.0)
            while "mouse" not in seen and time.perf_counter() < deadline:
                time.sleep(0.005)
            assert "mouse" in seen, "poller never saw tap %d" % i
            t_seen, cmd = seen.pop("mouse")
            poll_wait_ms.append((t_seen - t_queued) * 1000.0)
            # Daemon-measured visible response for this re-show.
            if daemon.last_switch_ms is not None:
                switch_ms.append(daemon.last_switch_ms)
            if daemon.last_frame_ms is not None:
                frame_ms.append(daemon.last_frame_ms)
            # Idle GET cost (today's short poll).
            ms, _ = http_get(base, "/macbook/mouse?since=%s"
                             % time.time())
            fetch_ms.append(ms)
            time.sleep(rng.uniform(0.05, args.interval))
        stop.set()
        pt.join(timeout=5.0)

        def stats(xs):
            xs = list(xs)
            if not xs:
                return {}
            out = {"n": len(xs), "min": min(xs), "max": max(xs),
                   "mean": statistics.mean(xs)}
            if len(xs) > 1:
                out["p50"] = statistics.median(xs)
                out["stdev"] = statistics.stdev(xs)
            return out

        result = {
            "mode": ("long-poll wait=%s" % args.wait) if args.wait
                    else "short-poll (today)",
            "taps": args.taps,
            "poller_interval_s": args.interval,
            "stage_A_resolve_ms": resolve_ms,
            "stage_A_post_roundtrip_ms": stats(post_ms),
            "stage_C_queue_to_fetch_ms": stats(poll_wait_ms),
            "idle_fetch_ms": stats(fetch_ms),
            "stage_D_request_to_present_ms": stats(switch_ms),
            "stage_D_request_to_fresh_draw_ms": stats(frame_ms),
        }
        draw = [f - s for f, s in zip(frame_ms, switch_ms)
                if f is not None and s is not None]
        result["stage_D_draw_only_ms"] = stats(draw)
        print(json.dumps(result, indent=2))
        if args.json_out:
            with open(args.json_out, "w") as fh:
                json.dump(result, fh, indent=2)
    finally:
        server.shutdown()
        thread.join()
        tmp.cleanup()


if __name__ == "__main__":
    main()
