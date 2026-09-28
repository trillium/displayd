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

Discipline, matching the renderer's latest-only contract:
  * drop frames rather than queue (one outstanding request; a late reply
    is discarded, never backlogged);
  * fps clamps to 0.5..5, mirroring ``renderers/stream.py``;
  * reconnect with backoff; log one line per STATE CHANGE, never per frame.

Transport (``obs_ws.py``), protocol pure functions (``obs_protocol.py``),
and CLI (``obs_poll_cli.py``, with the live checklist) are split out and
re-exported below for backwards compatibility. Exits non-zero only on
configuration errors; a dropped socket or an unreachable displayd is a
reconnect/retry, never a death.
"""

import json
import logging
import time
import urllib.error
import urllib.request
import uuid

from obs_protocol import (
    BACKOFF_FIRST,
    DEFAULT_FPS,  # noqa: F401 (re-exported for backwards compatibility)
    IMAGE_QUALITY,
    IMAGE_WIDTH,  # noqa: F401 (used by obs_poll_cli via obs_poll)
    MAX_FPS,  # noqa: F401 (re-exported for backwards compatibility)
    MIN_FPS,  # noqa: F401 (re-exported for backwards compatibility)
    REQUEST_TIMEOUT_PAD,
    clamp_fps,  # noqa: F401 (re-exported for backwards compatibility)
    next_backoff,
    obs_auth,  # noqa: F401 (re-exported for backwards compatibility)
    strip_data_prefix,  # noqa: F401 (re-exported for backwards compatibility)
)
from obs_ws import (
    ws_connect,
    ws_recv_text,  # noqa: F401 (re-exported; tests monkeypatch this name)
    ws_send_text,  # noqa: F401 (re-exported; tests monkeypatch this name)
)

LOG = logging.getLogger("obs-poll-bridge")


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

    def _set_state(self, state, msg, *args):
        if state == self._state:
            LOG.debug(msg, *args)
            return False
        self._state = state
        LOG.info(msg, *args)
        return True

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
            backoff, sleep = next_backoff(uptime, backoff)
            LOG.info("reconnecting in %.1fs (last connection %.0fs)",
                     sleep, uptime)
            time.sleep(sleep)


if __name__ == "__main__":
    import sys

    from obs_poll_cli import main

    sys.exit(main())
