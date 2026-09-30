#!/usr/bin/env python3
"""Talon apps <-> displayd bridge (poller, runs on the MacBook).

Sibling of ``macos_state.py`` (same poller shape, same launchd
supervision, stdlib only): each tick it (a) reads Talon's event-driven
``apps_state.json`` on mtime change and POSTs it to
``/feed/talon_apps/state`` for the merged macbook feature (the app list
lives in its GLANCE header; the namespace is owned there now, so this
endpoint is unchanged), and (b)
fetches one pending panel focus command (``GET /talon/focus?since=``),
hands it to Talon through the file protocol in
``core/displayd_apps/displayd_apps.py``, and waits for the response.

brain-15l95 note: the response wait below runs HERE, in this bridge
process -- never on Talon's serial main thread, and no copy of the
rpc_client blocking primitive is used. A slow Talon only delays this
tick; commands TTL-expire instead of firing late.

DURABLE PATH: the installed copy runs from the deployed tree
(~/displayd/bridges/talon_apps.py via deploy.sh), supervised by
bridges/com.displayd.talon-apps-bridge.plist -- never from a task
worktree.
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import time
import urllib.request

LOG = logging.getLogger("talon-apps-bridge")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import talon_windows
except Exception:  # Linux panel / tests: flat list, no placement
    talon_windows = None
try:
    import talon_channel as channel
except Exception:  # never: stdlib-only sibling, same directory
    channel = None

INTERVAL, TIMEOUT = 0.5, 5.0
WAIT_RESPONSE = 4.0
LONGPOLL_WAIT_MAX = 2.0  # fetch hold never exceeds this; the tick
# cadence (and its state POSTs) is preserved, taps just wake it early.
NAME_CHARS, MAX_APPS = 48, 30


def comm_dir(path=None):
    """Agree with the Talon side on the file-protocol directory."""
    if channel is not None:
        return channel.comm_dir(path)
    if path:
        return path
    suffix = "-%s" % os.getuid() if hasattr(os, "getuid") else ""
    return os.path.join(tempfile.gettempdir(), "displayd-apps%s" % suffix)


def clean(name):
    text = name if isinstance(name, str) else ""
    return "".join(ch for ch in text.strip() if ch.isprintable())[:NAME_CHARS]


def load_state(directory, now=None):
    """Validated (mtime, doc) for apps_state.json; (None, None) when
    absent or malformed. Never raises. `ts` is stamped here (bridge
    clock): Talon's write time only says when an app launched, while
    the panel's STALE tag must answer whether THIS bridge is alive."""
    path = os.path.join(directory, "apps_state.json")
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return None, None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as err:
        LOG.debug("state read failed: %s", err)
        return None, None
    if not isinstance(doc, dict) or not isinstance(doc.get("apps"), list):
        return None, None
    apps = [clean(a) for a in doc["apps"] if clean(a)][:MAX_APPS]
    out = {"ts": now if now is not None else time.time(),
            "apps": apps}
    if doc.get("focused"):
        out["focused"] = clean(doc["focused"])
    if talon_windows is not None:
        # Per-app screen placement (side-button grouping). Best
        # effort: any failure keeps the flat list, never the tick.
        try:
            owners, boxes = talon_windows.snapshot()
            if owners and boxes:
                placed = talon_windows.place(apps, owners, boxes)
                if placed:
                    out["windows"] = placed
                    out["displays"] = boxes
        except Exception as err:
            LOG.debug("window snapshot skipped: %s", err)
    return mtime, out


def post_feed(displayd_base, doc):
    """POST one state document to the panel feed; True on success."""
    url = displayd_base.rstrip("/") + "/feed/talon_apps/state"
    req = urllib.request.Request(url, data=json.dumps(doc).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read(1024)
    except Exception as err:
        LOG.info("displayd unreachable (%s); retrying", err)
        return False
    return True


def fetch_focus(displayd_base, since=0.0, wait=0.0):
    """Pending panel focus command newer than `since`; None when idle.
    `wait` holds the GET on the daemon (bounded server-side) so a tap
    queued mid-hold wakes this fetch instead of waiting for the next
    tick; 0 is today's immediate reply. The HTTP timeout always covers
    the hold, so a hung turn is impossible."""
    url = (displayd_base.rstrip("/") + "/talon/focus?since=%s" % since)
    if wait and wait > 0:
        url += "&wait=%s" % wait
    try:
        with urllib.request.urlopen(url,
                                     timeout=TIMEOUT + max(0.0, wait)) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as err:
        LOG.debug("focus fetch skipped (%s)", err)
        return None
    cmd = doc.get("command") if isinstance(doc, dict) else None
    if not isinstance(cmd, dict) or not cmd.get("app"):
        return None
    return cmd


def run_focus(directory, cmd):
    """Hand one command to Talon, wait for its answer (here, in this
    process). Returns the response doc or None on timeout. Files are
    consumed either way so nothing replays."""
    body = {"id": cmd.get("id"), "name": cmd.get("app"),
            "ts": cmd.get("ts", time.time())}
    if channel is None:
        return None
    resp = channel.exchange(directory, "focus_request.json",
                            "focus_response.json", body,
                            timeout=WAIT_RESPONSE)
    if resp is None:
        LOG.warning("focus command %r timed out waiting for Talon",
                    body["id"])
    return resp
    # NOTE: request file is consumed by Talon's tick (unlink on read);
    # the response file is unlinked by the caller after reading it.


HEARTBEAT = 2.0  # re-POST unchanged state this often. Must stay well
# under the daemon's FOCUS_FRESH gate (5s): the gate samples the feed
# at tap time, so a 10s heartbeat would leave it closed most ticks.


def tick(displayd_base, directory, seen, now=None, wait=0.0):
    """One poller tick. `seen` holds [state_mtime, focus_ts,
    last_post]. State POSTs on change plus a heartbeat, so the panel
    can tell a live-but-quiet bridge from a dead one. `wait` long-polls
    the focus fetch (tap wakes it); the state POST above still runs
    every tick, so cadence never degrades."""
    now = now if now is not None else time.time()
    mtime, doc = load_state(directory, now=now)
    if doc is not None and (mtime != seen[0]
                             or now - seen[2] > HEARTBEAT):
        if post_feed(displayd_base, doc):
            seen[0], seen[2] = mtime, now
    cmd = fetch_focus(displayd_base, seen[1], wait=wait)
    if cmd is None:
        return
    try:
        seen[1] = float(cmd.get("ts", seen[1]))
    except (TypeError, ValueError):
        pass
    resp = run_focus(directory, cmd)
    try:
        os.unlink(os.path.join(directory, "focus_response.json"))
    except FileNotFoundError:
        pass
    except Exception as err:
        LOG.debug("response cleanup failed: %s", err)
    LOG.info("focus %r -> %s", cmd.get("app"),
             resp if resp is not None else "no-answer")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Talon apps <-> displayd")
    ap.add_argument("--displayd", default=os.environ.get(
        "DISPLAYD_BASE", "http://100.81.88.113:8980"))
    ap.add_argument("--interval", type=float, default=float(
        os.environ.get("TALON_APPS_INTERVAL", str(INTERVAL))))
    ap.add_argument("--comm-dir", default=os.environ.get(
        "TALON_APPS_DIR", ""))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    directory = comm_dir(args.comm_dir or None)
    if args.once:
        mtime, doc = load_state(directory)
        print(json.dumps({"mtime": mtime, "state": doc},
                         indent=2)[:4000])
        return 0
    interval = min(max(float(args.interval), 0.25), 10.0)
    # Long-poll hold matches the tick: the loop keeps its cadence (state
    # POSTs and heartbeat unaffected) while taps wake the fetch at once.
    # After acting on a tap the next hold re-parks at once (0.05 floor),
    # so a back-to-back tap wakes instead of riding out the tick sleep.
    wait = min(interval, LONGPOLL_WAIT_MAX)
    seen = [None, 0.0, 0.0]
    while True:
        t0 = time.monotonic()
        focus_ts = seen[1]
        try:
            tick(args.displayd, directory, seen, wait=wait)
        except Exception as err:  # never die on a bad tick
            LOG.warning("apps tick failed: %s", err)
        if seen[1] != focus_ts:
            time.sleep(0.05)
        else:
            time.sleep(max(0.05, interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
