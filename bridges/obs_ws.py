"""Minimal WebSocket client (stdlib, transport only, no OBS knowledge).

Single concept: raw RFC 6455 text frames over a plain TCP socket --
connect, send one masked text message, receive one complete text message
(answering pings, skipping pongs, reassembling continuations).

Same shape as the client inside ``firebot_chat.py``; kept as a separate
copy so each bridge stays stdlib-only and dependency-free. The OBS
screenshot bridge (``obs_poll.py``) re-exports these names so existing
importers and test monkeypatches keep working.
"""

import base64
import os
import socket
import struct


def ws_connect(host, port, timeout=10):
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET / HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n" % (host, port, key))
    sock.sendall(req.encode())
    head = b""
    # One byte at a time: OBS sends Hello right behind the 101, and a
    # bulk recv would swallow those frame bytes along with the header.
    while not head.endswith(b"\r\n\r\n"):
        chunk = sock.recv(1)
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
    # via the caller's per-call settimeout (blocking here would hang
    # forever on a silent peer, a leftover timeout would break long polls).
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
