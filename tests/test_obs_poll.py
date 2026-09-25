"""OBS screenshot poller (bridges/obs_poll.py) tests.

Frame flow is proved against fakes at two levels: scripted in-memory
sockets for the protocol units, and a real TCP round-trip (hand-rolled
WebSocket server speaking obs-websocket v5) for the wire path, plus a
live push through the real stream renderer into a headless daemon feed.

Run from the repo root:  python3 -m unittest tests.test_obs_poll -v
"""

import base64
import hashlib
import io
import json
import logging
import os
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))

from PIL import Image

import displayd
from displayd import FeedStore, HeadlessFramebuffer, Screen

import obs_poll
from obs_poll import ObsPoller, clamp_fps, obs_auth, strip_data_prefix


def jpeg_bytes(w=160, h=90, color=(30, 120, 200)):
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


JPEG_B64 = base64.b64encode(jpeg_bytes()).decode()
DATA_URL = "data:image/jpeg;base64," + JPEG_B64


class ScriptedSock:
    """In-memory stand-in for a connected OBS socket.

    `incoming` is a queue of decoded text messages for ws_recv_text;
    everything the poller sends is captured in `sent`.
    """

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.timeout = None
        self.closed = False

    def settimeout(self, t):
        self.timeout = t

    def close(self):
        self.closed = True


def patch_ws(testcase, sock):
    """Route module ws helpers at one scripted socket. Returns sent list."""
    sent = sock.sent
    orig_send, orig_recv = obs_poll.ws_send_text, obs_poll.ws_recv_text

    def fake_send(s, text):
        assert s is sock
        sent.append(json.loads(text))

    def fake_recv(s):
        assert s is sock
        if not sock.incoming:
            raise TimeoutError("no more scripted messages")
        return sock.incoming.pop(0)

    testcase.addCleanup(setattr, obs_poll, "ws_send_text", orig_send)
    testcase.addCleanup(setattr, obs_poll, "ws_recv_text", orig_recv)
    obs_poll.ws_send_text = fake_send
    obs_poll.ws_recv_text = fake_recv
    return sent


def hello(auth=None):
    d = {"obsWebSocketVersion": "5.4.0", "rpcVersion": 1}
    if auth is not None:
        d["authentication"] = auth
    return json.dumps({"op": 0, "d": d})


def identified():
    return json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}})


def shot_response(request_id, image_data=DATA_URL, result=True):
    return json.dumps({"op": 7, "d": {
        "requestType": "GetSourceScreenshot",
        "requestId": request_id,
        "requestStatus": {"result": result, "code": 100 if result else 600},
        "responseData": {"imageData": image_data}}})


def make_poller(**kw):
    posted = []
    kw.setdefault("host", "obs")
    kw.setdefault("port", 4455)
    kw.setdefault("password", "")
    kw.setdefault("source", "Game")
    kw.setdefault("fps", 2)
    kw.setdefault("displayd", "http://displayd:8980")
    kw.setdefault("post_fn", posted.append)
    return ObsPoller(**kw), posted


class TestPureHelpers(unittest.TestCase):
    def test_auth_matches_protocol_spec(self):
        # Reference vector: recomputed here from the documented algorithm
        # sha256(pw+salt) -> b64 -> sha256(that+challenge) -> b64.
        pw, salt, challenge = "hunter2", "somesalt==", "somechallenge=="
        secret = base64.b64encode(
            hashlib.sha256((pw + salt).encode()).digest()).decode()
        expected = base64.b64encode(
            hashlib.sha256((secret + challenge).encode()).digest()).decode()
        self.assertEqual(obs_auth(pw, salt, challenge), expected)

    def test_fps_clamp_mirrors_renderer(self):
        self.assertEqual(clamp_fps(99), 5.0)
        self.assertEqual(clamp_fps(0), 0.5)
        self.assertEqual(clamp_fps(-3), 0.5)
        self.assertEqual(clamp_fps(None), 2.0)
        self.assertEqual(clamp_fps("junk"), 2.0)
        self.assertEqual(clamp_fps(1.5), 1.5)
        # Same caps the renderer enforces (single contract, two copies).
        import importlib.util
        path = os.path.join(os.path.dirname(__file__), os.pardir,
                            "renderers", "stream.py")
        spec = importlib.util.spec_from_file_location("stream_under_test",
                                                      path)
        stream = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stream)
        self.assertEqual((obs_poll.MIN_FPS, obs_poll.MAX_FPS,
                          obs_poll.DEFAULT_FPS),
                         (stream.MIN_FPS, stream.MAX_FPS,
                          stream.DEFAULT_FPS))

    def test_strip_data_prefix(self):
        self.assertEqual(strip_data_prefix(DATA_URL), JPEG_B64)
        self.assertEqual(strip_data_prefix(JPEG_B64), JPEG_B64)
        with self.assertRaises(ValueError):
            strip_data_prefix("")


class TestHandshake(unittest.TestCase):
    def test_no_auth_hello_identifies_without_secret(self):
        poller, _ = make_poller()
        sock = ScriptedSock([hello(), identified()])
        sent = patch_ws(self, sock)
        poller.handshake(sock)
        identify = sent[0]
        self.assertEqual(identify["op"], 1)
        self.assertNotIn("authentication", identify["d"])
        self.assertEqual(identify["d"]["eventSubscriptions"], 0)

    def test_auth_hello_answered_with_hash(self):
        poller, _ = make_poller(password="hunter2")
        auth = {"salt": "somesalt==", "challenge": "somechallenge=="}
        sock = ScriptedSock([hello(auth), identified()])
        sent = patch_ws(self, sock)
        poller.handshake(sock)
        self.assertEqual(sent[0]["d"]["authentication"],
                         obs_auth("hunter2", "somesalt==", "somechallenge=="))

    def test_password_required_but_missing_is_auth_failed(self):
        poller, _ = make_poller(password="")
        auth = {"salt": "s", "challenge": "c"}
        sock = ScriptedSock([hello(auth)])
        patch_ws(self, sock)
        with self.assertRaises(PermissionError):
            poller.handshake(sock)

    def test_identify_rejected_is_auth_failed(self):
        poller, _ = make_poller()
        sock = ScriptedSock([hello(), json.dumps({"op": 3, "d": {}})])
        patch_ws(self, sock)
        with self.assertRaises(PermissionError):
            poller.handshake(sock)


class TestGrab(unittest.TestCase):
    def test_grab_posts_request_and_returns_bare_frame(self):
        poller, _ = make_poller()
        sock = ScriptedSock([])  # reply appended once requestId is known
        sent = patch_ws(self, sock)
        orig_recv = obs_poll.ws_recv_text

        def recv_then_reply(s):
            req_id = sent[0]["d"]["requestId"]
            sock.incoming.append(shot_response(req_id))
            obs_poll.ws_recv_text = orig_recv
            return orig_recv(s)

        obs_poll.ws_recv_text = recv_then_reply
        frame = poller.grab(sock, timeout=5)
        self.assertEqual(frame, JPEG_B64)  # data: prefix stripped
        req = sent[0]["d"]
        self.assertEqual(req["requestType"], "GetSourceScreenshot")
        self.assertEqual(req["requestData"]["sourceName"], "Game")
        self.assertEqual(req["requestData"]["imageFormat"], "jpg")

    def test_stale_reply_ignored(self):
        poller, _ = make_poller()
        sock = ScriptedSock([])
        sent = patch_ws(self, sock)
        orig_recv = obs_poll.ws_recv_text
        calls = []

        def recv_skip_stale(s):
            calls.append(1)
            req_id = sent[0]["d"]["requestId"]
            if len(calls) == 1:
                return shot_response("some-older-request")
            obs_poll.ws_recv_text = orig_recv
            return shot_response(req_id)

        obs_poll.ws_recv_text = recv_skip_stale
        self.assertEqual(poller.grab(sock, timeout=5), JPEG_B64)
        self.assertEqual(len(calls), 2)

    def test_failed_status_raises(self):
        poller, _ = make_poller()
        sock = ScriptedSock([])
        sent = patch_ws(self, sock)
        orig_recv = obs_poll.ws_recv_text

        def recv_fail(s):
            obs_poll.ws_recv_text = orig_recv
            return shot_response(sent[0]["d"]["requestId"], result=False)

        obs_poll.ws_recv_text = recv_fail
        with self.assertRaises(RuntimeError):
            poller.grab(sock, timeout=5)


class TestStateChangeLogging(unittest.TestCase):
    def test_one_line_per_state_never_per_frame(self):
        poller, posted = make_poller()
        with self.assertLogs("obs-poll-bridge", level="INFO") as logs:
            for _ in range(5):
                poller.push(JPEG_B64)  # five frames, one state entry
            poller.push(JPEG_B64)
        streaming = [l for l in logs.output if "streaming" in l]
        self.assertEqual(len(streaming), 1)

    def test_displayd_outage_logs_once_then_recovers_once(self):
        calls = {"n": 0}

        def flaky(frame):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise ConnectionError("down")

        poller, _ = make_poller(post_fn=flaky)
        with self.assertLogs("obs-poll-bridge", level="INFO") as logs:
            self.assertFalse(poller.push(JPEG_B64))
            self.assertFalse(poller.push(JPEG_B64))
            self.assertFalse(poller.push(JPEG_B64))
            self.assertTrue(poller.push(JPEG_B64))
        out = "\n".join(logs.output)
        self.assertEqual(out.count("displayd unreachable"), 1)
        self.assertEqual(out.count("reachable again"), 1)

    def test_password_never_logged(self):
        poller, _ = make_poller(password="super-secret-pw")
        with self.assertLogs("obs-poll-bridge", level="DEBUG") as logs:
            poller._set_state("obs-unreachable", "OBS unreachable at %s:%d",
                              poller.host, poller.port)
            poller._set_state("auth-failed", "OBS auth failed: %s",
                              "bad password")
            poller.push(JPEG_B64)
        for line in logs.output:
            self.assertNotIn("super-secret-pw", line)


class TestServeOnce(unittest.TestCase):
    def test_unreachable_obs_returns_dropped(self):
        poller, _ = make_poller(
            connect_fn=lambda h, p: (_ for _ in ()).throw(
                ConnectionError("refused")))
        with self.assertLogs("obs-poll-bridge", level="INFO"):
            self.assertEqual(poller.serve_once(), "dropped")

    def test_full_cycle_against_scripted_obs(self):
        """Handshake + two paced screenshots -> two posted frames."""
        sock = ScriptedSock([hello(), identified()])
        poller, posted = make_poller(fps=50,  # clamped to 5; still fast
                                     connect_fn=lambda h, p: sock)
        self.assertEqual(poller.fps, 5.0)
        sent = patch_ws(self, sock)
        orig_recv = obs_poll.ws_recv_text
        ticks = {"n": 0}
        stop_after = 2

        def recv_script(s):
            if sock.incoming:
                return orig_recv(s)
            # A grab is waiting: answer the latest screenshot request.
            reqs = [m for m in sent if m.get("op") == 6]
            ticks["n"] += 1
            if ticks["n"] > stop_after:
                raise ConnectionError("test done")
            return shot_response(reqs[-1]["d"]["requestId"])

        obs_poll.ws_recv_text = recv_script
        with self.assertLogs("obs-poll-bridge", level="INFO"):
            poller.serve_once(stop=lambda: ticks["n"] > stop_after)
        frames = [p for p in posted]
        self.assertEqual(len(frames), 2)
        for frame in frames:
            self.assertEqual(base64.b64decode(frame), jpeg_bytes())


# ---- live wire: real TCP WebSocket server + real renderer -------------------

def _ws_accept(server_sock):
    conn, _ = server_sock.accept()
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = conn.recv(4096)
        if not chunk:
            raise ConnectionError("no handshake from client")
        head += chunk
    key = [l for l in head.decode("latin1").split("\r\n")
           if l.lower().startswith("sec-websocket-key")][0].split(":", 1)[1].strip()
    accept = base64.b64encode(hashlib.sha1(
        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n"
                  % accept).encode())
    return conn


def _ws_send(conn, text):
    data = text.encode()
    head = bytearray([0x81])
    if len(data) < 126:
        head.append(len(data))
    elif len(data) < 65536:
        head.append(126)
        head += struct.pack("!H", len(data))
    else:
        head.append(127)
        head += struct.pack("!Q", len(data))
    conn.sendall(bytes(head) + data)


def _ws_recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client went away")
        buf += chunk
    return buf


def _ws_recv(conn):
    hdr = _ws_recv_exact(conn, 2)
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _ws_recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _ws_recv_exact(conn, 8))[0]
    mask = _ws_recv_exact(conn, 4)
    payload = _ws_recv_exact(conn, length) if length else b""
    return bytes(b ^ mask[i % 4] for i, b in enumerate(payload)).decode()


class FakeObsServer(threading.Thread):
    """Real TCP socket speaking obs-websocket v5: hello, identify check,
    then answer every GetSourceScreenshot with a real JPEG."""

    def __init__(self, password=""):
        super().__init__(daemon=True)
        self.password = password
        self.requests = []
        self.identify = None
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.ready = threading.Event()

    def run(self):
        self.ready.set()
        conn = _ws_accept(self.sock)
        try:
            hello_d = {"obsWebSocketVersion": "5.4.0", "rpcVersion": 1}
            if self.password:
                salt, challenge = "livesalt==", "livechallenge=="
                hello_d["authentication"] = {"salt": salt,
                                             "challenge": challenge}
            _ws_send(conn, json.dumps({"op": 0, "d": hello_d}))
            self.identify = json.loads(_ws_recv(conn))
            if self.password:
                want = obs_auth(self.password, salt, challenge)
                if self.identify["d"].get("authentication") != want:
                    return  # wrong password: drop, like OBS does
            _ws_send(conn, json.dumps({"op": 2,
                                       "d": {"negotiatedRpcVersion": 1}}))
            while True:
                msg = json.loads(_ws_recv(conn))
                self.requests.append(msg)
                d = msg.get("d") or {}
                _ws_send(conn, shot_response(d.get("requestId")))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


class TestLiveWire(unittest.TestCase):
    def test_against_real_tcp_obs_server(self):
        server = FakeObsServer()
        server.start()
        server.ready.wait(5)
        poller, posted = make_poller(host="127.0.0.1", port=server.port,
                                     fps=5)
        stop = time.monotonic() + 3
        with self.assertLogs("obs-poll-bridge", level="INFO"):
            poller.run_forever(stop=lambda: time.monotonic() > stop)
        self.assertGreaterEqual(len(posted), 2,
                                "expected frames through the real socket")
        for frame in posted:
            self.assertEqual(base64.b64decode(frame), jpeg_bytes())
        req = server.requests[0]["d"]
        self.assertEqual(req["requestType"], "GetSourceScreenshot")
        self.assertEqual(req["requestData"]["sourceName"], "Game")
        server.sock.close()

    def test_wrong_password_gets_no_frames(self):
        server = FakeObsServer(password="correct")
        server.start()
        server.ready.wait(5)
        poller, posted = make_poller(host="127.0.0.1", port=server.port,
                                     password="wrong", fps=5)
        with self.assertLogs("obs-poll-bridge", level="INFO") as logs:
            outcome = poller.serve_once()
        self.assertEqual(posted, [])
        # Real OBS drops the socket on bad auth, surfacing here as a
        # failed handshake; either way no frame flows and it is logged.
        self.assertIn(outcome, ("dropped", "auth-failed"))
        self.assertTrue(logs.output, "expected a state-change log line")
        server.sock.close()

    def test_frame_reaches_real_renderer(self):
        """A polled frame, pushed through the real feed path, presents."""
        screen = Screen(HeadlessFramebuffer(width=320, height=180))
        screen.feeds = FeedStore()
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        stream = found["stream"]["module"]
        spec = found["stream"]["inputs"]["frame"]
        screen.feeds.push("stream", "frame", {"data": JPEG_B64}, spec)
        stop = threading.Event()
        runner = threading.Thread(target=stream.run, args=(screen, {}, stop),
                                  daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while screen.fb.last_frame is None and time.monotonic() < deadline:
            time.sleep(0.05)
        stop.set()
        runner.join(5)
        self.assertIsNotNone(screen.fb.last_frame,
                             "renderer never presented the pushed frame")
        self.assertEqual(len(screen.fb.last_frame), 320 * 180 * 4)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
