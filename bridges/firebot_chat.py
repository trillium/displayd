#!/usr/bin/env python3
"""Firebot -> displayd chat bridge.

Firebot (on the captain's MacBook) already emits every chat message, fully
enriched, over its unauthenticated local WebSocket. This bridge subscribes to
that stream and pushes messages across the tailnet into displayd's feed API::

    Firebot ws://<firebot-host>:7472/ --WS--> bridge --HTTP--> displayd

The bridge owns the vendor protocol; displayd stays content-agnostic and only
ever sees validated ``{author, text, ...}`` payloads on ``/feed/chat/message``
(plus ``{messageId}`` retractions on ``/feed/chat/delete``), and the present
viewer list on ``/feed/chat/roster``.

Presence comes from a second, slower loop beside the socket: one GET of
Firebot's own viewer list per cycle (``firebot_roster.py`` explains which
endpoint and why), and that single read answers both questions the panel asks
-- the roster the left pane draws, and the arrivals that ride the chat feed as
join events. Joins are announced; departures are not (the roster pane drops
them, and the request named joins).

Protocol notes (from the Firebot scout report -- do not re-derive):
  * hello is ``overlay-connected`` with ``{"instanceName": "Stream 1080p"}``;
  * the server broadcasts ALL overlay traffic to every subscriber, so filter
    strictly on overlayInstance + widgetType.id + event names;
  * an unregistered socket is dropped after ~5 s, so the hello goes out
    immediately on every (re)connect;
  * reconnect with backoff; each message is deduped on chatMessage.id because
    it arrives in both `state-update` snapshots and `message` increments.

Stdlib only, no credentials anywhere (the socket needs none). Exits non-zero
only on configuration errors; a dropped socket is a reconnect, never a death.
"""

import argparse
import json
import logging
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request

LOG = logging.getLogger("firebot-chat-bridge")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import firebot_wire as _wire
import firebot_roster as _roster

# Re-exported for backwards compatibility (Firebot upstream wire protocol
# lives in firebot_wire.py; the bridge lifecycle below is the only
# in-repo consumer besides the tests).
SILENCE_LIMIT = _wire.SILENCE_LIMIT
WIDGET_ID = _wire.WIDGET_ID
OVERLAY_INSTANCE = _wire.OVERLAY_INSTANCE
ws_handshake = _wire.ws_handshake
ws_send_text = _wire.ws_send_text
_recv_exact = _wire._recv_exact
ws_recv_texts = _wire.ws_recv_texts
_widget_event_data = _wire._widget_event_data
extract_chat = _wire.extract_chat
normalize = _wire.normalize

INSTANCE = "Stream 1080p"

HELLO = {"type": "invoke", "id": 1, "name": "overlay-connected",
         "data": [{"instanceName": INSTANCE}]}

BACKOFF_FIRST = 1.0
BACKOFF_MAX = 30.0
SEEN_CAP = 1000

# How often the viewer list is read. Firebot's own presence TTL is 7.5
# minutes (a 5-minute Helix chatter poll, plus chat activity), so a tighter
# cadence buys nothing but load, and a slower one delays a join showing up.
ROSTER_INTERVAL = 10.0


# ---- bridge ------------------------------------------------------------------

class Bridge:
    def __init__(self, firebot_host, firebot_port, displayd_base,
                 roster_url=None, roster_interval=ROSTER_INTERVAL):
        self.firebot_host = firebot_host
        self.firebot_port = firebot_port
        self.displayd_base = displayd_base.rstrip("/")
        self.roster = _roster.Roster(roster_url or _roster.url_for(
            firebot_host, firebot_port))
        self.presence = _roster.Presence(self.roster, self.post,
                                         roster_interval)
        self.roster_interval = float(roster_interval)
        self.seen = set()
        self.seen_order = []
        self.connects = 0

    # -- seen-set ------------------------------------------------------
    def fresh(self, mid):
        if not mid or mid in self.seen:
            return False
        self.seen.add(mid)
        self.seen_order.append(mid)
        while len(self.seen_order) > SEEN_CAP:
            self.seen.discard(self.seen_order.pop(0))
        return True

    # -- displayd ------------------------------------------------------
    def post(self, input_name, payload):
        url = "%s/feed/chat/%s" % (self.displayd_base, input_name)
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            LOG.warning("displayd rejected %s: HTTP %d %s",
                        input_name, err.code, err.read(200).decode("replace"))
        except Exception as err:
            LOG.warning("displayd POST %s failed: %s", input_name, err)
        return None

    def check_firebot(self):
        try:
            with urllib.request.urlopen(
                    "http://%s:%d/api/v1/status" % (self.firebot_host, self.firebot_port),
                    timeout=6) as resp:
                status = json.loads(resp.read().decode())
            chat = (status.get("connections") or {}).get("chat")
            LOG.info("firebot status: connections.chat=%r", chat)
        except Exception as err:
            LOG.warning("firebot status check failed: %s", err)

    # -- events --------------------------------------------------------
    def handle_raw(self, raw):
        try:
            env = json.loads(raw)
        except ValueError:
            LOG.debug("non-JSON frame (%d bytes)", len(raw))
            return 0
        if not isinstance(env, dict):
            return 0
        if env.get("type") == "response":
            # Registration acknowledgement -- the proof the hello landed.
            LOG.info("firebot response: %s", raw[:200])
            return 0
        delivered = 0
        for kind, item in extract_chat(env):
            if kind == "message":
                msg = normalize(item)
                LOG.info("chat: %s", json.dumps(item, separators=(",", ":"))[:2000])
                if self.fresh(msg["id"]) and msg["text"]:
                    self.post("message", msg)
                    delivered += 1
            elif kind == "backfill":
                for m in item:
                    msg = normalize(m)
                    if self.fresh(msg["id"]) and msg["text"]:
                        self.post("message", msg)
                        delivered += 1
                if delivered:
                    LOG.info("backfill: delivered %d message(s)", delivered)
            elif kind == "delete":
                self.post("delete", item)
                delivered += 1
        return delivered

    # -- lifecycle -----------------------------------------------------
    def serve_once(self, stop=None):
        """One connection: hello immediately, then consume until drop."""
        sock = ws_handshake(self.firebot_host, self.firebot_port)
        self.connects += 1
        LOG.info("connected to firebot %s:%d (connection #%d); registering",
                 self.firebot_host, self.firebot_port, self.connects)
        try:
            ws_send_text(sock, json.dumps(HELLO))  # re-sent EVERY reconnect
            LOG.info("hello sent (overlay-connected -> %r)", INSTANCE)
            n = 0
            for raw in ws_recv_texts(sock, stop=stop):
                n += self.handle_raw(raw)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        if stop is not None and stop():
            return "stopped"
        LOG.warning("socket dropped after %d delivered message(s); reconnecting", n)
        return "dropped"

    def run_forever(self, stop=None):
        self.check_firebot()
        # Presence rides its own thread: the socket half keeps running
        # whether or not a viewer-list read works.
        presence = threading.Thread(target=self.presence.forever, args=(stop,),
                                    name="firebot-roster", daemon=True)
        presence.start()
        backoff = BACKOFF_FIRST
        while True:
            if stop is not None and stop():
                return
            t0 = time.monotonic()
            try:
                outcome = self.serve_once(stop=stop)
            except Exception as err:
                LOG.warning("connection failed: %s", err)
                outcome = "failed"
            if outcome == "stopped":
                return
            uptime = time.monotonic() - t0
            if uptime > 30:
                backoff = BACKOFF_FIRST  # healthy connection; reset the ladder
            else:
                backoff = min(backoff * 2, BACKOFF_MAX)
            sleep = min(backoff, BACKOFF_MAX) * (0.8 + 0.4 * random.random())
            LOG.info("reconnecting in %.1fs (last connection %.0fs)", sleep, uptime)
            time.sleep(sleep)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Firebot -> displayd chat bridge")
    ap.add_argument("--firebot-host", default=os.environ.get("FIREBOT_HOST", "100.74.138.74"),
                    help="Firebot host (default: %(default)s)")
    ap.add_argument("--firebot-port", type=int, default=int(os.environ.get("FIREBOT_PORT", "7472")),
                    help="Firebot port (default: %(default)s)")
    ap.add_argument("--displayd", default=os.environ.get("DISPLAYD_BASE", "http://100.81.88.113:8980"),
                    help="displayd base URL (default: %(default)s)")
    _roster.add_arguments(ap)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not args.firebot_host or not args.displayd:
        ap.error("firebot host and displayd base URL are required")
        return 2
    Bridge(args.firebot_host, args.firebot_port, args.displayd,
           roster_url=args.roster_url,
           roster_interval=args.roster_interval).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
