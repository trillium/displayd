"""PM5 bridge WebSocket transport for the row view. NOT a renderer: no run(),
so the daemon's loader skips this file (same convention as beads_common.py).

Owns the minimal stdlib-only WS client that reads the mini1 PM5 bridge
socket OBS also uses -- the same feed, never a second serving path.
Server frames are unmasked, ping is answered, close ends the read.
fetch_ws_stats returns None when the feed is reachable but quiet (PM5
idle, nothing fresh): a live, healthy silence, not a failure.
"""

import base64
import hashlib
import json
import os
import socket
import ssl
import time
import urllib.parse

WS_MAX_BYTES = 1024 * 1024
WS_MAX_MESSAGES = 50

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_dial_url(url):
    """Map an http(s) .../ws URL onto its ws(s) twin (same OBS socket)."""
    parts = urllib.parse.urlsplit(url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    return urllib.parse.urlunsplit(
        (scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def _recv_exact(sock, n, deadline, lookahead=None):
    buf = b""
    if lookahead:
        buf = bytes(lookahead[:n])
        del lookahead[:n]
    while len(buf) < n:
        if time.monotonic() > deadline:
            raise TimeoutError("row feed read timed out")
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            raise TimeoutError("row feed read timed out")
        if not chunk:
            raise OSError("row feed closed mid-frame")
        buf += chunk
    return buf


def _ws_pong(sock, payload):
    try:
        if len(payload) < 126:
            sock.sendall(b"\x8a" + bytes([len(payload)]) + payload)
        else:
            sock.sendall(b"\x8a\x7e" + len(payload).to_bytes(2, "big") + payload)
    except OSError:
        pass


def fetch_ws_stats(url, timeout):
    """Connect to the PM5 /obs/ws feed and return the first stats dict.
    Minimal stdlib-only WS client: server frames are unmasked, ping is
    answered, close ends the read. Returns None when the feed is reachable
    but quiet (handshake OK, no stats within the timeout: PM5 idle with
    nothing fresh to say) -- that is a live, healthy silence, not a
    failure. Raises OSError/ValueError/TimeoutError on connect/handshake
    failure, an explicit close, or oversize frames."""
    target = _ws_dial_url(url) if url.startswith(("http://", "https://")) else url
    parts = urllib.parse.urlsplit(target)
    if parts.scheme not in ("ws", "wss"):
        raise ValueError("row stats url must be ws(s) or http(s) .../ws")
    host = parts.hostname or ""
    if not host:
        raise ValueError("row stats url has no host")
    port = parts.port or (443 if parts.scheme == "wss" else 80)
    resource = parts.path or "/"
    if parts.query:
        resource += "?" + parts.query
    deadline = time.monotonic() + max(1, timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(max(0.5, deadline - time.monotonic()))
        if parts.scheme == "wss":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
            sock.settimeout(max(0.5, deadline - time.monotonic()))
        req = (
            "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\nUser-Agent: displayd-row/1\r\n\r\n"
        ) % (resource, parts.netloc or host, key)
        sock.sendall(req.encode("latin-1"))
        head = b""
        status_end = head.find(b"\r\n\r\n")
        lookahead = bytearray()
        while status_end < 0:
            if time.monotonic() > deadline or len(head) > 16384:
                raise OSError("row feed handshake failed")
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                raise TimeoutError("row feed handshake timed out")
            if not chunk:
                raise OSError("row feed handshake failed")
            head += chunk
            status_end = head.find(b"\r\n\r\n")
        header_block = head[:status_end]
        # A fast server pipelines the greeting frame into the same TCP
        # segment as the handshake: keep those bytes for frame parsing.
        lookahead = bytearray(head[status_end + 4:])
        status = header_block.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise OSError("row feed handshake rejected: %s"
                          % status.decode("latin-1", "replace")[:80])
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if accept not in header_block.decode("latin-1", "replace"):
            raise OSError("row feed handshake key mismatch")
        text_parts = []
        waiting_cont = False
        seen = 0
        total = 0
        while seen < WS_MAX_MESSAGES:
            if time.monotonic() > deadline:
                return None  # reachable but quiet: PM5 idle, nothing fresh
            sock.settimeout(max(0.5, deadline - time.monotonic()))
            try:
                hdr = _recv_exact(sock, 2, deadline, lookahead)
            except TimeoutError:
                return None  # reachable but quiet (see above)
            fin = bool(hdr[0] & 0x80)
            opcode = hdr[0] & 0x0F
            masked = bool(hdr[1] & 0x80)
            length = hdr[1] & 0x7F
            if length == 126:
                length = int.from_bytes(_recv_exact(sock, 2, deadline, lookahead), "big")
            elif length == 127:
                length = int.from_bytes(_recv_exact(sock, 8, deadline, lookahead), "big")
            if length > WS_MAX_BYTES:
                raise ValueError("row feed frame too large")
            mask = _recv_exact(sock, 4, deadline, lookahead) if masked else b""
            payload = _recv_exact(sock, length, deadline, lookahead) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close: explicit server action, not quiet
                raise OSError("row feed closed")
            if opcode == 0x9:  # ping -> pong, keep listening
                _ws_pong(sock, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x1:
                text_parts = [payload]
                waiting_cont = not fin
            elif opcode == 0x0 and text_parts:
                text_parts.append(payload)
                waiting_cont = not fin
            else:
                continue
            if waiting_cont:
                continue
            seen += 1
            total += sum(len(p) for p in text_parts)
            if total > WS_MAX_BYTES:
                raise ValueError("row feed message too large")
            try:
                msg = json.loads(
                    b"".join(text_parts).decode("utf-8", errors="replace"))
            except ValueError:
                text_parts = []
                continue
            text_parts = []
            if isinstance(msg, dict) and (
                    msg.get("type") == "stats" or
                    (msg.get("type") is None and isinstance(msg.get("raw"), dict))):
                return msg
        return None  # messages flowed, but none were stats: quiet
    finally:
        try:
            sock.close()
        except Exception:
            pass
