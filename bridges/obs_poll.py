#!/usr/bin/env python3
"""OBS -> displayd stream bridge (screenshot poller).

OBS already exposes every source over its unauthenticated-or-passworded
local WebSocket (protocol v5). This bridge connects to that socket,
requests a screenshot of one source on a cadence, and pushes each frame
across the tailnet into displayd's feed API::

    OBS ws://<obs-host>:4455 --WS--> bridge --HTTP--> displayd /feed/stream/frame

The bridge owns the vendor protocol; displayd stays content-agnostic and
only ever sees validated ``{"data": "<base64 jpeg>"}`` payloads on
``/feed/stream/frame`` (``renderers/stream.py``).

Protocol notes (obs-websocket 5.x -- do not re-derive):
  * server opens with Hello ``{"op": 0, "d": {"rpcVersion": 1, ...}}``;
    when a password is set, ``d.authentication`` carries salt+challenge;
  * answer with Identify ``{"op": 1, ...}`` (eventSubscriptions 0: this
    bridge wants request/response only, no events); server replies
    Identified ``{"op": 2, ...}``;
  * screenshot is request ``GetSourceScreenshot`` (op 6), answered by op 7
    with ``responseData.imageData`` as ``data:image/...;base64,....``.

Discipline, matching the renderer's latest-only contract:
  * drop frames rather than queue (one outstanding request; a late reply
    is discarded, never backlogged);
  * fps clamps to 0.5..5, mirroring ``renderers/stream.py``;
  * reconnect with backoff; log one line per STATE CHANGE, never per frame.

Stdlib only, no credentials in code or logs: the password travels via the
``OBS_PASSWORD`` environment variable only (there is deliberately no
``--password`` flag, so it never leaks through ``ps``), and no log line
ever includes it. Exits non-zero only on configuration errors; a dropped
socket or an unreachable displayd is a reconnect/retry, never a death.

Live checklist against real OBS (run once, then leave the bridge running):
  1. ``OBS_SOURCE='<name>' OBS_PASSWORD='<pw>' ./bridges/obs_poll.py --verbose``
     shows ``connected`` + ``streaming`` and the panel leaves its
     "waiting for stream" idle screen for the source image.
  2. Wrong password shows exactly one ``auth-failed`` line and retries
     with backoff (fix the env var, it recovers on its own).
  3. Kill OBS: exactly one ``obs-unreachable`` line; restart OBS and the
     bridge reconnects without a restart.
  4. Stop displayd: exactly one ``displayd-unreachable`` line, frames drop,
     panel keeps its last-good frame; restart displayd and pushes resume.
  5. ``POST /show {"renderer": "stream"}`` on the panel, then confirm the
     measured fps on screen matches the configured ``--fps``.
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
import uuid

LOG = logging.getLogger("obs-poll-bridge")

# Renderer caps, mirrored from renderers/stream.py (single source of truth
# for the cap lives in the renderer; this copy only keeps the poller from
# out-pacing what the panel can present).
MIN_FPS = 0.5
MAX_FPS = 5.0
DEFAULT_FPS = 2.0

BACKOFF_FIRST = 1.0
BACKOFF_MAX = 30.0
HEALTHY_RESET_AFTER = 30.0  # a connection living this long resets the ladder
REQUEST_TIMEOUT_PAD = 2.0   # extra seconds beyond one frame interval
IMAGE_WIDTH = 960           # screenshot width cap: keeps frames panel-sized
IMAGE_QUALITY = 70          # jpeg quality: small frames, still readable


# ---- minimal WebSocket client (stdlib; same shape as firebot_chat.py) -------

def ws_connect(host, port, timeout=10):
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
    # create_connection leaves the connect timeout on the socket; the
    # handshake runs bounded by it, then reads go fully deadline-driven
    # via grab()'s per-call settimeout (blocking here would hang forever
    # on a silent peer, a leftover timeout would break long polls).
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


def ws_recv_text(sock):
    """One complete text message; answers pings; raises on close/error."""
    pending = bytearray()
    while True:
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
                msg, pending = bytes(pending), bytearray()
                return msg.decode("utf-8", "replace")
            continue
        if op in (0x1, 0x2):
            if fin:
                return payload.decode("utf-8", "replace")
            pending = bytearray(payload)
            continue
        # unknown opcode: ignore


# ---- obs-websocket v5 ---------------------------------------------------------

def obs_auth(password, salt, challenge):
    """Authentication string for Identify. Pure function (test hook)."""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode()).digest()).decode()


def clamp_fps(raw):
    """Mirror of renderers/stream.py _clamp_fps (pure function, test hook)."""
    try:
        fps = float(raw if raw is not None else DEFAULT_FPS)
    except (TypeError, ValueError):
        return DEFAULT_FPS
    return max(MIN_FPS, min(MAX_FPS, fps))


def strip_data_prefix(image_data):
    """'data:image/jpeg;base64,....' -> raw base64 (what /feed wants)."""
    if not isinstance(image_data, str) or not image_data:
        raise ValueError("empty screenshot payload")
    if "," in image_data and image_data.startswith("data:"):
        return image_data.split(",", 1)[1]
    return image_data


class ObsPoller:
    """One bridge: OBS screenshots -> displayd /feed/stream/frame."""

    def __init__(self, host, port, password, source, fps, displayd,
                 image_width=IMAGE_WIDTH, connect_fn=None, post_fn=None):
        if not source:
            raise ValueError("source name is required")
        self.host = host
        self.port = port
        self.password = password or ""
        self.source = source
        self.fps = clamp_fps(fps)
        self.displayd = displayd.rstrip("/")
        self.image_width = image_width
        self._connect_fn = connect_fn or ws_connect  # test seam
        self._post_fn = post_fn or self._post_http   # test seam
        self._state = None  # last logged state; None = nothing logged yet
        self.frames = 0
        self.dropped = 0

    # -- state-change-only logging --------------------------------------
    def _set_state(self, state, msg, *args):
        if state == self._state:
            LOG.debug(msg, *args)
            return False
        self._state = state
        LOG.info(msg, *args)
        return True

    # -- protocol --------------------------------------------------------
    def handshake(self, sock):
        """Consume Hello, answer Identify. Raises on auth failure."""
        hello = json.loads(ws_recv_text(sock))
        if not isinstance(hello, dict) or hello.get("op") != 0:
            raise ConnectionError("expected Hello, got %r" % (hello,)[:1])
        data = hello.get("d") or {}
        identify = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
        auth = data.get("authentication")
        if auth:
            if not self.password:
                raise PermissionError(
                    "OBS requires a password (set OBS_PASSWORD)")
            identify["d"]["authentication"] = obs_auth(
                self.password, auth["salt"], auth["challenge"])
        ws_send_text(sock, json.dumps(identify))
        reply = json.loads(ws_recv_text(sock))
        if not isinstance(reply, dict) or reply.get("op") != 2:
            raise PermissionError("OBS rejected Identify: %r" % (reply,)[:1])
        return reply

    def grab(self, sock, timeout):
        """One screenshot -> raw base64 frame. Raises on any failure."""
        request_id = uuid.uuid4().hex
        ws_send_text(sock, json.dumps({
            "op": 6,
            "d": {
                "requestType": "GetSourceScreenshot",
                "requestId": request_id,
                "requestData": {
                    "sourceName": self.source,
                    "imageFormat": "jpg",
                    "imageWidth": self.image_width,
                    "imageCompressionQuality": IMAGE_QUALITY,
                },
            },
        }))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("screenshot reply too slow; dropping frame")
            sock.settimeout(remaining)
            msg = json.loads(ws_recv_text(sock))
            if not isinstance(msg, dict) or msg.get("op") != 7:
                continue  # events are off, but never trust the wire
            data = msg.get("d") or {}
            if data.get("requestId") != request_id:
                continue  # stale reply from a dropped frame; ignore it
            status = data.get("requestStatus") or {}
            if not status.get("result"):
                raise RuntimeError("OBS screenshot failed: %r" % (status,))
            image = (data.get("responseData") or {}).get("imageData")
            return strip_data_prefix(image)

    # -- displayd push ----------------------------------------------------
    def _post_http(self, b64_frame):
        body = json.dumps({"data": b64_frame}).encode()
        req = urllib.request.Request(
            self.displayd + "/feed/stream/frame", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read(1024)

    def push(self, b64_frame):
        """POST one frame; True on success, False when displayd is down."""
        try:
            self._post_fn(b64_frame)
        except Exception as err:
            self._set_state("displayd-unreachable",
                            "displayd unreachable (%s); dropping frames", err)
            self.dropped += 1
            return False
        if self._state == "displayd-unreachable":
            self._set_state("streaming",
                            "displayd reachable again; streaming %r at %.1f fps",
                            self.source, self.fps)
        elif self._state != "streaming":
            self._set_state("streaming", "streaming %r at %.1f fps",
                            self.source, self.fps)
        self.frames += 1
        return True

    # -- lifecycle ---------------------------------------------------------
    def serve_once(self, stop=None):
        """One connection: handshake, then screenshot on the fps cadence."""
        try:
            sock = self._connect_fn(self.host, self.port)
        except Exception as err:
            self._set_state("obs-unreachable",
                            "OBS unreachable at %s:%d (%s); retrying",
                            self.host, self.port, err)
            return "dropped"
        try:
            self.handshake(sock)
        except PermissionError as err:
            self._set_state("auth-failed", "OBS auth failed: %s", err)
            try:
                sock.close()
            except Exception:
                pass
            return "auth-failed"
        except Exception as err:
            self._set_state("obs-unreachable",
                            "OBS handshake failed (%s); retrying", err)
            try:
                sock.close()
            except Exception:
                pass
            return "dropped"
        try:
            sock.settimeout(None)  # handshake window over; grab() paces reads
        except Exception:
            pass
        self._set_state("connected", "connected to OBS %s:%d; streaming %r",
                        self.host, self.port, self.source)
        interval = 1.0 / self.fps
        try:
            while True:
                if stop is not None and stop():
                    return "stopped"
                tick = time.monotonic()
                try:
                    frame = self.grab(sock, interval + REQUEST_TIMEOUT_PAD)
                except (TimeoutError, RuntimeError, ValueError) as err:
                    # Late or bad reply: drop this frame, keep the socket.
                    LOG.debug("dropped frame: %s", err)
                    self.dropped += 1
                    continue
                self.push(frame)
                # Pace at the fps cap (request cost counts against it).
                elapsed = time.monotonic() - tick
                time.sleep(max(0.01, interval - elapsed))
        except Exception as err:
            self._set_state("obs-unreachable", "OBS socket dropped (%s)", err)
            return "dropped"
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def run_forever(self, stop=None):
        backoff = BACKOFF_FIRST
        while True:
            if stop is not None and stop():
                return
            t0 = time.monotonic()
            try:
                outcome = self.serve_once(stop=stop)
            except Exception as err:  # never die on a bad tick
                LOG.warning("poll tick failed: %s", err)
                outcome = "failed"
            if outcome == "stopped":
                return
            uptime = time.monotonic() - t0
            if uptime > HEALTHY_RESET_AFTER:
                backoff = BACKOFF_FIRST  # healthy run; reset the ladder
            else:
                backoff = min(backoff * 2, BACKOFF_MAX)
            sleep = min(backoff, BACKOFF_MAX) * (0.8 + 0.4 * random.random())
            LOG.info("reconnecting in %.1fs (last connection %.0fs)",
                     sleep, uptime)
            time.sleep(sleep)


def main(argv=None):
    ap = argparse.ArgumentParser(description="OBS -> displayd stream bridge")
    ap.add_argument("--obs-host",
                    default=os.environ.get("OBS_HOST", "100.74.138.74"),
                    help="OBS host (default: %(default)s)")
    ap.add_argument("--obs-port", type=int,
                    default=int(os.environ.get("OBS_PORT", "4455")),
                    help="obs-websocket v5 port (default: %(default)s)")
    ap.add_argument("--source", default=os.environ.get("OBS_SOURCE", ""),
                    help="OBS source name to screenshot (or OBS_SOURCE)")
    ap.add_argument("--fps", type=float,
                    default=float(os.environ.get("OBS_FPS", str(DEFAULT_FPS))),
                    help="poll rate, clamped to 0.5..5 (default: %(default)s)")
    ap.add_argument("--displayd",
                    default=os.environ.get("DISPLAYD_BASE",
                                           "http://100.81.88.113:8980"),
                    help="displayd base URL (default: %(default)s)")
    ap.add_argument("--width", type=int, default=IMAGE_WIDTH,
                    help="screenshot width cap (default: %(default)s)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # NOTE: the password comes from OBS_PASSWORD only -- no CLI flag, so it
    # never appears in ps output, and it is never logged anywhere above.
    if not args.source:
        ap.error("source name is required (--source or OBS_SOURCE)")
        return 2
    if not args.displayd:
        ap.error("displayd base URL is required")
        return 2
    ObsPoller(args.obs_host, args.obs_port, os.environ.get("OBS_PASSWORD", ""),
              args.source, args.fps, args.displayd,
              image_width=args.width).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
