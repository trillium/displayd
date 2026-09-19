"""Feed mechanism (INPUTS + /feed) and chat renderer tests.

No framebuffer needed for the store/validation/renderer parts. The bridge
test runs a fake Firebot WebSocket server on loopback that drops the first
connection, proving the bridge re-registers and keeps delivering.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd
from displayd import FeedStore, validate_params, validate_value

CHAT_MSG = {"id": "msg-1", "author": "testuser", "display_name": "TestUser",
            "text": "hello wall", "color": "#2E8B57", "badges": [],
            "timestamp": 1789801169829, "isMod": False, "isSubscriber": True,
            "isVip": False, "isFirstChat": True}


def chat_spec():
    found = displayd.load_renderers(displayd.RENDERER_DIR)
    return found["chat"]["inputs"]["message"]


class TestInputsAdvertised(unittest.TestCase):
    def test_chat_declares_inputs(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("chat", found)
        inputs = found["chat"]["inputs"]
        self.assertIn("message", inputs)
        self.assertIn("delete", inputs)
        self.assertEqual(inputs["message"]["type"], "object")

    def test_renderers_without_inputs_are_unchanged(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        for name in ("text", "image", "solid", "clock", "life"):
            self.assertIn(name, found)
            self.assertEqual(found[name]["inputs"], {},
                             "%r declares no inputs" % name)

    def test_input_schemas_are_well_formed(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        for name, entry in found.items():
            if "module" not in entry:
                continue
            for iname, spec in (entry.get("inputs") or {}).items():
                self.assertIsInstance(spec, dict)
                self.assertIn(spec.get("type"), displayd.INPUT_TYPES,
                              "%r.%r unknown type" % (name, iname))

    def test_renderer_list_advertises_inputs(self):
        import inspect
        src = inspect.getsource(displayd.DisplayDaemon.renderer_list)
        self.assertIn("inputs", src)

    def test_handler_routes_feed(self):
        import inspect
        src = inspect.getsource(displayd.Handler.do_POST)
        self.assertIn("/feed/", src)


class TestFeedValidation(unittest.TestCase):
    def test_valid_message_passes(self):
        validate_value(dict(CHAT_MSG), chat_spec(), "chat.message")

    def test_minimal_message_passes(self):
        validate_value({"author": "a", "text": "hi"}, chat_spec(), "chat.message")

    def test_missing_required_rejected(self):
        with self.assertRaises(ValueError):
            validate_value({"author": "a"}, chat_spec(), "chat.message")

    def test_wrong_type_rejected(self):
        bad = dict(CHAT_MSG, text=123)
        with self.assertRaises(ValueError):
            validate_value(bad, chat_spec(), "chat.message")

    def test_unknown_extra_keys_allowed(self):
        msg = dict(CHAT_MSG, futureField={"x": 1}, emotes=["Kappa"])
        validate_value(msg, chat_spec(), "chat.message")

    def test_delete_schema(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        spec = found["chat"]["inputs"]["delete"]
        validate_value({"messageId": "msg-1"}, spec, "chat.delete")
        with self.assertRaises(ValueError):
            validate_value({"nope": 1}, spec, "chat.delete")

    def test_non_object_rejected(self):
        with self.assertRaises(ValueError):
            validate_value([1, 2], chat_spec(), "chat.message")

    def test_params_still_validated(self):
        with self.assertRaises(ValueError):
            validate_params({}, {"text": {"type": "string", "required": True}})
        validate_params({"text": "hi"}, {"text": {"type": "string", "required": True}})


class TestFeedStore(unittest.TestCase):
    def test_push_and_get_are_ordered(self):
        store = FeedStore()
        spec = chat_spec()
        store.push("chat", "message", dict(CHAT_MSG, id="1"), spec)
        store.push("chat", "message", dict(CHAT_MSG, id="2"), spec)
        got = store.get("chat", "message")
        self.assertEqual([m["id"] for m in got], ["1", "2"])

    def test_invalid_push_does_not_disturb_buffer(self):
        store = FeedStore()
        spec = chat_spec()
        store.push("chat", "message", dict(CHAT_MSG), spec)
        with self.assertRaises(ValueError):
            store.push("chat", "message", {"author": "x"}, spec)
        got = store.get("chat", "message")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["id"], "msg-1")

    def test_unknown_feed_reads_empty(self):
        self.assertEqual(FeedStore().get("nope", "nothing"), [])

    def test_snapshot_health(self):
        store = FeedStore()
        store.declare("chat", "message", chat_spec())
        snap = store.snapshot()
        self.assertEqual(snap["chat"]["message"]["health"], "cold")
        store.push("chat", "message", dict(CHAT_MSG), chat_spec())
        snap = store.snapshot()
        self.assertEqual(snap["chat"]["message"]["health"], "warm")
        self.assertEqual(snap["chat"]["message"]["count"], 1)

    def test_buffer_cap(self):
        store = FeedStore()
        spec = dict(chat_spec(), buffer=3)
        for i in range(5):
            store.push("chat", "message", {"author": "a", "text": "t%d" % i}, spec)
        self.assertEqual(len(store.get("chat", "message")), 3)


class FakeFb:
    def __init__(self, w=960, h=540):
        self.width, self.height = w, h
        self.frames = []

    def present(self, img):
        self.frames.append(img.copy())


class TestChatRenderer(unittest.TestCase):
    def make_screen(self):
        screen = displayd.Screen(FakeFb())
        store = FeedStore()
        store.declare("chat", "message", chat_spec())
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        store.declare("chat", "delete", found["chat"]["inputs"]["delete"])
        screen.feeds = store
        return screen, store

    def test_snapshot_applies_deletes_and_dedupes(self):
        screen, store = self.make_screen()
        mod = store  # noqa - keep linters quiet about unused
        _ = mod
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "chatmod", os.path.join(displayd.RENDERER_DIR, "chat.py"))
        chat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(chat)
        spec_msg = chat.INPUTS["message"]
        spec_del = chat.INPUTS["delete"]
        store.push("chat", "message", dict(CHAT_MSG), spec_msg)
        store.push("chat", "message", dict(CHAT_MSG), spec_msg)  # duplicate id
        store.push("chat", "message", dict(CHAT_MSG, id="msg-2", text="second"), spec_msg)
        self.assertEqual(len(chat._snapshot(screen)), 2)
        store.push("chat", "delete", {"messageId": "msg-1"}, spec_del)
        snap = chat._snapshot(screen)
        self.assertEqual([m["id"] for m in snap], ["msg-2"])

    def test_run_renders_buffered_messages_without_reselect(self):
        """Messages pushed while the view is NOT running appear on the first
        frame after it starts -- the buffering requirement."""
        screen, store = self.make_screen()
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "chatmod2", os.path.join(displayd.RENDERER_DIR, "chat.py"))
        chat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(chat)
        # Feed while "not selected": no renderer thread is running.
        store.push("chat", "message", dict(CHAT_MSG), chat.INPUTS["message"])
        store.push("chat", "message", dict(CHAT_MSG, id="msg-2", text="second line",
                                           author="other"), chat.INPUTS["message"])
        stop = threading.Event()
        thread = threading.Thread(target=chat.run, args=(screen, {}, stop), daemon=True)
        thread.start()
        try:
            deadline = time.time() + 5
            painted = None
            while time.time() < deadline:
                if screen.fb.frames:
                    img = screen.fb.frames[-1]
                    # Count pixels that differ from the near-black background.
                    small = img.resize((160, 90)).convert("L")
                    if sum(1 for p in small.getdata() if p > 24) > 60:
                        painted = img
                        break
                time.sleep(0.1)
            self.assertIsNotNone(painted, "chat view never painted its buffered messages")
        finally:
            stop.set()
            thread.join(timeout=5)


# ---- bridge reconnect test ----------------------------------------------------

def _ws_server_frame(text):
    data = text.encode("utf-8")
    head = bytearray([0x81])
    if len(data) < 126:
        head.append(len(data))
    elif len(data) < 65536:
        head.append(126)
        head += struct.pack("!H", len(data))
    else:
        head.append(127)
        head += struct.pack("!Q", len(data))
    return bytes(head) + data


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _read_client_frame(sock):
    hdr = _read_exact(sock, 2)
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(sock, 8))[0]
    key = _read_exact(sock, 4)
    payload = _read_exact(sock, length)
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload)).decode("utf-8")


def chat_envelope(kind="chat-message"):
    inner = {"widgetType": {"id": "firebot:chat"}, "widgetConfig": {},
             "messageName": kind, "messageData": {}}
    if kind == "chat-message":
        inner["messageData"] = {"chatMessage": {
            "id": "live-1", "username": "testuser", "userDisplayName": "TestUser",
            "rawText": "live hello", "timestamp": 1789801169829, "color": "#2E8B57",
            "badges": [], "isMod": False, "isSubscriber": True, "isVip": False,
            "isFirstChat": False}}
    else:
        inner["messageData"] = {"messageId": "live-1", "animate": True}
    return {"type": "event", "name": "send-to-overlay",
            "data": {"event": "OVERLAY:WIDGET-EVENT",
                     "meta": {"event": {"name": "message", "data": inner}},
                     "overlayInstance": "Stream 1080p"}}


class FakeFirebot:
    """Minimal overlay-protocol server: records hellos, sends one chat message
    then drops the first connection to force a bridge reconnect."""

    def __init__(self):
        self.hellos = []
        self.ws_conns = 0
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()

    def _serve(self):
        conn_no = 0
        conns = []
        try:
            while not self.stop_event.is_set():
                try:
                    conn, _ = self.sock.accept()
                except socket.timeout:
                    continue
                conn_no += 1
                conns.append(conn)
                try:
                    self._handle(conn, conn_no)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
        finally:
            for c in conns:
                try:
                    c.close()
                except Exception:
                    pass

    def _handle(self, conn, conn_no):
        conn.settimeout(5)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(4096)
            if not chunk:
                return
            head += chunk
        if b"Upgrade: websocket" not in head:
            # Plain HTTP (e.g. the bridge's REST status probe): answer it.
            body = b'{"connections":{"chat":true}}'
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" +
                          ("Content-Length: %d\r\n\r\n" % len(body)).encode() + body)
            return
        self.ws_conns += 1
        ws_no = self.ws_conns
        key = [l for l in head.decode("latin1").split("\r\n")
               if l.lower().startswith("sec-websocket-key")][0].split(":", 1)[1].strip()
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n"
                      % accept).encode())
        hello = json.loads(_read_client_frame(conn))
        self.hellos.append(hello)
        if ws_no == 1:
            conn.sendall(_ws_server_frame(json.dumps(chat_envelope("chat-message"))))
            conn.sendall(_ws_server_frame(json.dumps(chat_envelope("delete-message"))))
            time.sleep(0.3)
            conn.close()  # abrupt drop: the bridge must re-register
        else:
            # stay up until the test ends
            while not self.stop_event.is_set():
                time.sleep(0.05)

    def shutdown(self):
        self.stop_event.set()
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=2).close()
        except Exception:
            pass
        self.thread.join(timeout=5)
        self.sock.close()


class CapturingDisplayd(BaseHTTPRequestHandler):
    posts = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"connections":{"chat":true}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        CapturingDisplayd.posts.append((self.path, self.rfile.read(length)))
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestBridgeReconnect(unittest.TestCase):
    def test_bridge_rehellos_and_delivers_across_a_drop(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "bridges"))
        import firebot_chat
        firebot_chat.BACKOFF_FIRST = 0.05
        firebot_chat.BACKOFF_MAX = 0.2
        CapturingDisplayd.posts = []
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), CapturingDisplayd)
        st = threading.Thread(target=httpd.serve_forever, daemon=True)
        st.start()
        firebot = FakeFirebot()
        firebot.start()
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            bridge = firebot_chat.Bridge("127.0.0.1", firebot.port, base)
            stop = threading.Event()
            bt = threading.Thread(target=bridge.run_forever,
                                  kwargs={"stop": stop.is_set}, daemon=True)
            bt.start()
            deadline = time.time() + 12
            while time.time() < deadline:
                if len(firebot.hellos) >= 2 and len(CapturingDisplayd.posts) >= 2:
                    break
                time.sleep(0.1)
            stop.set()
            bt.join(timeout=5)
            # Re-registered after the drop: one hello per connection.
            self.assertGreaterEqual(len(firebot.hellos), 2,
                                    "bridge did not re-hello after the drop")
            for hello in firebot.hellos:
                self.assertEqual(hello.get("name"), "overlay-connected")
                self.assertEqual(hello["data"][0]["instanceName"], "Stream 1080p")
            paths = [p for p, _ in CapturingDisplayd.posts]
            self.assertIn("/feed/chat/message", paths)
            self.assertIn("/feed/chat/delete", paths)
            msg = json.loads([b for p, b in CapturingDisplayd.posts
                              if p == "/feed/chat/message"][0])
            self.assertEqual(msg["author"], "testuser")
            self.assertEqual(msg["text"], "live hello")
        finally:
            try:
                stop.set()
            except NameError:
                pass
            firebot.shutdown()
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
