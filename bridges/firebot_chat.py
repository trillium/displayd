#!/usr/bin/env python3
"""Firebot -> displayd chat bridge.

Firebot (on the captain's MacBook) already emits every chat message, fully
enriched, over its unauthenticated local WebSocket. This bridge subscribes to
that stream and pushes messages across the tailnet into displayd's feed API::

    Firebot ws://<firebot-host>:7472/ --WS--> bridge --HTTP--> displayd

The bridge owns the vendor protocol; displayd stays content-agnostic and only
ever sees validated ``{author, text, ...}`` payloads on ``/feed/chat/message``
(plus ``{messageId}`` retractions on ``/feed/chat/delete``).

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
import base64
import hashlib
import json
import logging
import os
import random
import socket
import struct
import sys
import time
import urllib.error
import urllib.request

LOG = logging.getLogger("firebot-chat-bridge")

INSTANCE = "Stream 1080p"
WIDGET_ID = "firebot:chat"
OVERLAY_INSTANCE = "Stream 1080p"

HELLO = {"type": "invoke", "id": 1, "name": "overlay-connected",
         "data": [{"instanceName": INSTANCE}]}

BACKOFF_FIRST = 1.0
BACKOFF_MAX = 30.0
SEEN_CAP = 1000
SILENCE_LIMIT = 180.0  # idle-but-healthy connections persist; past this, re-hello+resync


# ---- minimal WebSocket client (stdlib; Firebot speaks plain ws://) ----------

def ws_handshake(host, port, timeout=10):
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET / HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n" % (host, port, key))
    sock.sendall(req.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("handshake: connection closed")
        head += chunk
        if len(head) > 65536:
            raise ConnectionError("handshake: header too large")
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        raise ConnectionError("handshake failed: %r" % status[:80])
    accept = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    if base64.b64encode(accept).decode() not in head.decode("latin1"):
        LOG.warning("handshake accept-key mismatch (continuing anyway)")
    # Handshake done: stop enforcing the connect timeout. An idle channel is
    # healthy -- quiet chat sends nothing for minutes. Reads past
    # SILENCE_LIMIT raise socket.timeout, which the serve loop treats as a
    # cue to re-hello and resync (backfill), never as an error.
    sock.settimeout(SILENCE_LIMIT)
    return sock


def ws_send_text(sock, text):
    data = text.encode("utf-8")
    mask = os.urandom(4)
    head = bytearray([0x81])
    n = len(data)
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head.append(0x80 | 126)
        head += struct.pack("!H", n)
    else:
        head.append(0x80 | 127)
        head += struct.pack("!Q", n)
    head += mask
    sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def ws_recv_texts(sock, stop=None):
    """Yield complete text messages; answer pings; raise on close/error."""
    pending_op, pending = None, bytearray()
    while True:
        if stop is not None and stop():
            return
        hdr = _recv_exact(sock, 2)
        fin = hdr[0] & 0x80
        op = hdr[0] & 0x0F
        masked = hdr[1] & 0x80
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
        key = _recv_exact(sock, 4) if masked else None
        payload = _recv_exact(sock, length) if length else b""
        if key:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        if op == 0x8:  # close
            raise ConnectionError("server closed the socket")
        if op == 0x9:  # ping -> pong
            pong = bytearray([0x8A, 0x80 | min(length, 125)])
            pong += os.urandom(4)
            mask = pong[-4:]
            pong += bytes(b ^ mask[i % 4] for i, b in enumerate(payload[:125]))
            sock.sendall(bytes(pong))
            continue
        if op == 0xA:  # pong
            continue
        if op == 0x0:  # continuation
            pending += payload
            if fin:
                msg, pending, pending_op = bytes(pending), bytearray(), None
                yield msg.decode("utf-8", "replace")
            continue
        if op in (0x1, 0x2):
            if fin:
                yield payload.decode("utf-8", "replace")
            else:
                pending_op, pending = op, bytearray(payload)
            continue
        # unknown opcode: ignore


# ---- Firebot event filtering -------------------------------------------------

def _widget_event_data(env):
    """Pull (overlay_instance, event_name, event_data) from a send-to-overlay
    envelope, or (None, None, None)."""
    try:
        if env.get("type") != "event" or env.get("name") != "send-to-overlay":
            return None, None, None
        data = env.get("data") or {}
        if data.get("event") != "OVERLAY:WIDGET-EVENT":
            return None, None, None
        meta = data.get("meta") or {}
        event = meta.get("event") or {}
        edata = event.get("data") or {}
        if (edata.get("widgetType") or {}).get("id") != WIDGET_ID:
            return None, None, None
        return data.get("overlayInstance"), event.get("name"), edata
    except AttributeError:
        return None, None, None


def extract_chat(env):
    """Strict predicate chain from the scout report. Returns a list of
    ("message", chatMessage) / ("delete", messageData) / ("backfill", [msgs]).

    Live increments arrive as message/chat-message; every envelope from our
    widget -- message, state-update, or the show snapshot sent on connect --
    is also scanned for widgetConfig.state.chatMessages (last <=100), so a
    (re)connect resyncs without special cases. Dedupe on id downstream."""
    out = []
    overlay, name, edata = _widget_event_data(env)
    if edata is None or overlay != OVERLAY_INSTANCE:
        return out
    state = ((edata.get("widgetConfig") or {}).get("state") or {}).get("chatMessages")
    if isinstance(state, list):
        msgs = [m for m in state if isinstance(m, dict) and m.get("rawText") is not None]
        if msgs:
            out.append(("backfill", msgs))
    if name != "message":
        return out
    kind = edata.get("messageName")
    mdata = edata.get("messageData") or {}
    if kind == "chat-message" and isinstance(mdata.get("chatMessage"), dict):
        out.append(("message", mdata["chatMessage"]))
    elif kind == "delete-message" and isinstance(mdata.get("messageId"), str):
        out.append(("delete", {"messageId": mdata["messageId"]}))
    return out


def normalize(chat):
    """Firebot's enriched chatMessage -> displayd feed payload. Defensive:
    roles/badges/parts shapes vary with emotes, cheers, replies."""
    badges = []
    for b in chat.get("badges") or []:
        if isinstance(b, str):
            badges.append(b)
        elif isinstance(b, dict):
            for k in ("type", "id", "name"):
                if b.get(k):
                    badges.append(str(b[k]))
                    break
        if len(badges) >= 8:
            break
    return {
        "id": str(chat.get("id") or ""),
        "author": str(chat.get("username") or "???"),
        "display_name": str(chat.get("userDisplayName") or chat.get("username") or "???"),
        "text": str(chat.get("rawText") or ""),
        "color": str(chat.get("color") or ""),
        "badges": badges,
        "timestamp": chat.get("timestamp") or 0,
        "isMod": bool(chat.get("isMod")),
        "isSubscriber": bool(chat.get("isSubscriber")),
        "isVip": bool(chat.get("isVip")),
        "isFirstChat": bool(chat.get("isFirstChat")),
    }


# ---- bridge ------------------------------------------------------------------

class Bridge:
    def __init__(self, firebot_host, firebot_port, displayd_base):
        self.firebot_host = firebot_host
        self.firebot_port = firebot_port
        self.displayd_base = displayd_base.rstrip("/")
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
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not args.firebot_host or not args.displayd:
        ap.error("firebot host and displayd base URL are required")
        return 2
    Bridge(args.firebot_host, args.firebot_port, args.displayd).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
