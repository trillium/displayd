"""Tests for the row (Concept2 streak) wall view.

Streak math mirrors row.sh's rest-day bank rule; these tests pin the
edge cases: covered rest day, streak break on an empty bank, empty log,
garbage lines, year reset, and future rows. Rendering tests use a
fontless FakeScreen (CI has no fonts) and only assert nonblank frames.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import base64
import datetime
import hashlib
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "renderers"))

import displayd

from PIL import Image

import row as row_view


D = datetime.date


def counts_from(days):
    """{date: rows} helper: days is [(iso, n), ...]."""
    out = {}
    for iso, n in days:
        out[D.fromisoformat(iso)] = n
    return out


def log_text(days):
    """rows.txt-style text for [(iso, n), ...]."""
    lines = []
    for iso, n in days:
        for i in range(n):
            lines.append("%sT12:00:%02d-08:00" % (iso, i))
    return "\n".join(lines) + "\n"


class FakeScreen:
    W, H = 1920, 1080

    def __init__(self):
        self.frames = []

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        self.frames.append(img.copy())

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        return displayd.Screen.color(value, default)

    @staticmethod
    def font_path(family="DejaVuSans-Bold"):
        return None  # CI box has no fonts: renderers must survive that


def _nonblank(img):
    extrema = img.getextrema()
    return any(lo != hi for lo, hi in extrema)


class TestParseLog(unittest.TestCase):
    def test_counts_rows_per_day(self):
        counts, last, total = row_view.parse_log(log_text([("2026-01-01", 2), ("2026-01-02", 1)]))
        self.assertEqual(counts, {D(2026, 1, 1): 2, D(2026, 1, 2): 1})
        self.assertEqual(total, 3)
        self.assertTrue(last.startswith("2026-01-02"))

    def test_garbage_lines_ignored(self):
        text = "??\n\n   \n" + log_text([("2026-01-01", 1)]) + "not-a-date\n2026-13-99T99:99:99-08:00\n"
        counts, last, total = row_view.parse_log(text)
        self.assertEqual(counts, {D(2026, 1, 1): 1})
        self.assertEqual(total, 1)

    def test_empty_log(self):
        self.assertEqual(row_view.parse_log(""), ({}, None, 0))
        self.assertEqual(row_view.parse_log("??\n??\n"), ({}, None, 0))


class TestStreakMath(unittest.TestCase):
    def test_consecutive_days(self):
        c = counts_from([("2026-01-01", 1), ("2026-01-02", 1), ("2026-01-03", 1)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 3)), (3, 3, 0))

    def test_extra_rows_build_bank(self):
        c = counts_from([("2026-01-01", 3)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 1)), (1, 3, 2))

    def test_covered_rest_day_holds(self):
        # Bank of 1 covers the missed 01-02: day streak grows, rows unchanged.
        c = counts_from([("2026-01-01", 2), ("2026-01-03", 1)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 3)), (3, 3, 0))

    def test_rest_day_without_bank_breaks(self):
        c = counts_from([("2026-01-01", 1)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 2)), (0, 0, 0))

    def test_two_misses_on_bank_of_one_breaks(self):
        c = counts_from([("2026-01-01", 2)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 3)), (0, 0, 0))

    def test_fresh_start_after_break(self):
        c = counts_from([("2026-01-01", 1), ("2026-01-05", 2)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 5)), (1, 2, 1))

    def test_empty_log_is_zero(self):
        self.assertEqual(row_view.compute_streaks({}, D(2026, 1, 5)), (0, 0, 0))

    def test_future_rows_ignored(self):
        c = counts_from([("2026-01-01", 1), ("2026-06-01", 1)])
        self.assertEqual(row_view.compute_streaks(c, D(2026, 1, 1)), (1, 1, 0))

    def test_year_boundary_resets(self):
        c = counts_from([("2025-12-31", 2), ("2026-01-01", 1)])
        ds, rs, bank = row_view.compute_streaks(c, D(2026, 1, 1))
        self.assertEqual((ds, rs, bank), (1, 1, 0))

    def test_bank_invariant(self):
        # bank == rows - days whenever the streak is alive.
        c = counts_from([("2026-01-01", 2), ("2026-01-03", 3), ("2026-01-04", 1)])
        ds, rs, bank = row_view.compute_streaks(c, D(2026, 1, 4))
        self.assertGreater(ds, 0)
        self.assertEqual(bank, rs - ds)


class TestSummarize(unittest.TestCase):
    def test_rowed_today(self):
        c = counts_from([("2026-01-01", 1), ("2026-01-02", 1)])
        s = row_view.summarize(c, "2026-01-02T12:00:00-08:00", 2, as_of=D(2026, 1, 2))
        self.assertEqual(s["status"], "rowed today")
        self.assertEqual(s["day_streak"], 2)
        self.assertEqual(s["rows_year"], 2)
        self.assertEqual(s["pace"], 2 - 2)

    def test_rest_day_held(self):
        c = counts_from([("2026-01-01", 2)])
        s = row_view.summarize(c, "2026-01-01T12:00:00-08:00", 2, as_of=D(2026, 1, 2))
        self.assertEqual(s["status"], "rest — streak held")
        self.assertEqual(s["day_streak"], 2)
        self.assertEqual(s["row_streak"], 2)

    def test_broken(self):
        c = counts_from([("2026-01-01", 1)])
        s = row_view.summarize(c, "2026-01-01T12:00:00-08:00", 1, as_of=D(2026, 1, 2))
        self.assertEqual(s["status"], "streak broken")
        self.assertEqual(s["day_streak"], 0)

    def test_empty(self):
        s = row_view.summarize({}, None, 0, as_of=D(2026, 1, 2))
        self.assertEqual(s["status"], "no rows yet")
        self.assertEqual((s["day_streak"], s["row_streak"], s["bank"]), (0, 0, 0))


class TestPathResolution(unittest.TestCase):
    def test_explicit_param_wins(self):
        self.assertEqual(row_view.resolve_path("/tmp/custom-rows.txt"), "/tmp/custom-rows.txt")

    def test_env_var_used(self):
        old = os.environ.get(row_view.ENV_VAR)
        os.environ[row_view.ENV_VAR] = "/tmp/env-rows.txt"
        try:
            self.assertEqual(row_view.resolve_path(), "/tmp/env-rows.txt")
        finally:
            if old is None:
                del os.environ[row_view.ENV_VAR]
            else:
                os.environ[row_view.ENV_VAR] = old

    def test_no_hardcoded_home_paths(self):
        with open(row_view.__file__) as fh:
            src = fh.read()
        for marker in ("/Users/", "/home/", "/root/", "$HOME", "expanduser"):
            self.assertNotIn(marker, src, "hardcoded home reference %r" % marker)


class TestContract(unittest.TestCase):
    def test_renderer_loads_and_advertises(self):
        found = displayd.load_renderers(displayd.RENDERER_DIR)
        self.assertIn("row", found)
        self.assertIn("module", found["row"], found["row"].get("broken"))
        mod = found["row"]["module"]
        self.assertFalse(mod.STATIC)
        self.assertTrue(callable(mod.run))
        self.assertIn("path", mod.PARAMS)
        for pname, spec in mod.PARAMS.items():
            self.assertIn(spec.get("type"), ("string", "integer", "number", "boolean"))

    def test_missing_file_frame_is_not_blank(self):
        screen = FakeScreen()
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        # Hermetic: file-kind source + scratch journal, no network.
        with tempfile.TemporaryDirectory() as tmp:
            row_view.run(screen, {
                "path": "/tmp/does-not-exist-rows.txt",
                "source": "/tmp/does-not-exist-rows.txt",
                "journal": os.path.join(tmp, "j.json"),
                "interval": 5,
            }, stop)
        self.assertTrue(screen.frames, "missing log must still present a frame")
        self.assertTrue(_nonblank(screen.frames[-1]))

    def test_empty_log_frame_is_not_blank(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("??\n\n")
            name = fh.name
        try:
            screen = FakeScreen()
            stop = threading.Event()
            threading.Timer(0.2, stop.set).start()
            with tempfile.TemporaryDirectory() as tmp:
                row_view.run(screen, {
                    "path": name,
                    "source": name,
                    "journal": os.path.join(tmp, "j.json"),
                    "interval": 5,
                }, stop)
            self.assertTrue(screen.frames)
            self.assertTrue(_nonblank(screen.frames[-1]))
        finally:
            os.unlink(name)

    def test_populated_log_renders_and_reports_streak(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(log_text([("2026-01-01", 2), ("2026-01-02", 1)]))
            name = fh.name
        try:
            snap = row_view.read_snapshot(name)
            # Dates are historical; only the shape matters, not liveness.
            self.assertEqual(snap["rows_year"], 3)
            screen = FakeScreen()
            stop = threading.Event()
            threading.Timer(0.2, stop.set).start()
            with tempfile.TemporaryDirectory() as tmp:
                row_view.run(screen, {
                    "path": name,
                    "source": name,
                    "journal": os.path.join(tmp, "j.json"),
                    "interval": 5,
                }, stop)
            self.assertTrue(screen.frames)
            self.assertTrue(_nonblank(screen.frames[-1]))
            self.assertEqual(screen.frames[-1].size, (1920, 1080))
        finally:
            os.unlink(name)


class TestSourceParams(unittest.TestCase):
    def test_classify_ws_shapes(self):
        self.assertEqual(row_view.classify_source("ws://mini1:8765/obs/ws"),
                         ("ws", "ws://mini1:8765/obs/ws"))
        self.assertEqual(row_view.classify_source("wss://h:1/ws"), ("ws", "wss://h:1/ws"))
        # http(s) URLs onto the OBS socket stay on the same feed.
        self.assertEqual(row_view.classify_source("http://mini1:8765/obs/ws")[0], "ws")
        self.assertEqual(row_view.classify_source("https://mini1:8765/obs/ws")[0], "ws")

    def test_classify_text_and_file_shapes(self):
        self.assertEqual(row_view.classify_source("http://h/rows.txt")[0], "text-url")
        self.assertEqual(row_view.classify_source("https://h/x/rows")[0], "text-url")
        self.assertEqual(row_view.classify_source("/tmp/rows.txt"), ("file", "/tmp/rows.txt"))
        self.assertEqual(row_view.classify_source(""), ("file", ""))

    def test_resolve_source_precedence(self):
        self.assertEqual(row_view.resolve_source(), row_view.DEFAULT_SOURCE)
        self.assertIn("mini1", row_view.DEFAULT_SOURCE)
        self.assertEqual(row_view.resolve_source("/tmp/x.txt"), "/tmp/x.txt")
        old = os.environ.get(row_view.ENV_SOURCE)
        os.environ[row_view.ENV_SOURCE] = "ws://other:1/ws"
        try:
            self.assertEqual(row_view.resolve_source(), "ws://other:1/ws")
            self.assertEqual(row_view.resolve_source("/tmp/x.txt"), "/tmp/x.txt")
        finally:
            if old is None:
                del os.environ[row_view.ENV_SOURCE]
            else:
                os.environ[row_view.ENV_SOURCE] = old

    def test_parse_timeout_clamps(self):
        self.assertEqual(row_view.parse_timeout(None), 10)
        self.assertEqual(row_view.parse_timeout("junk"), 10)
        self.assertEqual(row_view.parse_timeout("20"), 20)
        self.assertEqual(row_view.parse_timeout(1), 2)
        self.assertEqual(row_view.parse_timeout(999), 60)

    def test_resolve_journal_precedence(self):
        self.assertEqual(row_view.resolve_journal("/tmp/j.json"), "/tmp/j.json")
        old = os.environ.get(row_view.ENV_JOURNAL)
        os.environ[row_view.ENV_JOURNAL] = "/tmp/env-j.json"
        try:
            self.assertEqual(row_view.resolve_journal(), "/tmp/env-j.json")
        finally:
            if old is None:
                del os.environ[row_view.ENV_JOURNAL]
            else:
                os.environ[row_view.ENV_JOURNAL] = old
        nxt = row_view.resolve_journal(None, "/tmp/logs/rows.txt")
        self.assertEqual(nxt, os.path.join(os.path.abspath("/tmp/logs"), "row_sightings.json"))
        fallback = row_view.resolve_journal()
        self.assertTrue(fallback.startswith(tempfile.gettempdir()))

    def test_source_label(self):
        self.assertEqual(row_view.source_label("ws", "ws://mini1:8765/obs/ws", "/x/rows.txt"),
                         "live ws://mini1:8765/obs/ws + /x/rows.txt")
        self.assertEqual(row_view.source_label("file", "/x/rows.txt", "/x/rows.txt"),
                         "/x/rows.txt")
        self.assertEqual(row_view.source_label("file", "", None), "no log configured")

    def test_remote_params_advertised(self):
        for pname in ("source", "timeout", "journal"):
            self.assertIn(pname, row_view.PARAMS)


class TestSighting(unittest.TestCase):
    def _msg(self, **kw):
        raw = {"distance_m": 1500.0, "elapsed_time_s": 400.0, "stroke_rate": 22}
        raw.update(kw.pop("raw", {}))
        msg = {"type": "stats", "connected": True, "raw": raw}
        msg.update(kw)
        return msg

    def test_active_workout_sighted(self):
        ok, sample = row_view.stats_sighting(self._msg())
        self.assertTrue(ok)
        self.assertEqual(sample, {"distance_m": 1500.0, "elapsed_time_s": 400.0})

    def test_disconnected_never_sights(self):
        ok, sample = row_view.stats_sighting(self._msg(connected=False))
        self.assertFalse(ok)
        self.assertIsNone(sample)

    def test_idle_zeros_do_not_sight(self):
        ok, _s = row_view.stats_sighting(
            self._msg(raw={"distance_m": 0.0, "elapsed_time_s": 0.0}))
        self.assertFalse(ok)

    def test_warmup_distance_does_not_sight(self):
        ok, _s = row_view.stats_sighting(
            self._msg(raw={"distance_m": 50.0, "elapsed_time_s": 30.0}))
        self.assertFalse(ok)

    def test_legacy_message_without_connected_sights(self):
        msg = self._msg()
        del msg["connected"]
        ok, _s = row_view.stats_sighting(msg)
        self.assertTrue(ok)

    def test_malformed_messages_do_not_sight(self):
        self.assertEqual(row_view.stats_sighting(None), (False, None))
        self.assertEqual(row_view.stats_sighting({"type": "stats"}), (False, None))
        self.assertEqual(row_view.stats_sighting(
            {"connected": True, "raw": {"distance_m": "far"}}), (False, None))

    def test_frozen_sample_does_not_resight(self):
        ok, sample = row_view.stats_sighting(self._msg())
        self.assertTrue(ok)
        ok2, sample2 = row_view.stats_sighting(self._msg(), sample)
        self.assertFalse(ok2)
        self.assertEqual(sample2, sample)
        moved = self._msg(raw={"distance_m": 1520.0, "elapsed_time_s": 410.0})
        ok3, _s3 = row_view.stats_sighting(moved, sample)
        self.assertTrue(ok3)


class TestJournal(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            jp = os.path.join(tmp, "j.json")
            row_view.save_journal(jp, {"2026-09-22"}, {"distance_m": 1.0})
            days, last = row_view.load_journal(jp)
            self.assertEqual(days, {"2026-09-22"})
            self.assertEqual(last, {"distance_m": 1.0})

    def test_missing_or_corrupt_reads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            days, last = row_view.load_journal(os.path.join(tmp, "nope.json"))
            self.assertEqual((days, last), (set(), None))
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w") as fh:
                fh.write("{not json")
            self.assertEqual(row_view.load_journal(bad), (set(), None))

    def test_note_sighting_idempotent_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            jp = os.path.join(tmp, "j.json")
            sample = {"distance_m": 1500.0, "elapsed_time_s": 400.0}
            self.assertTrue(row_view.note_sighting(jp, "2026-09-22", sample))
            self.assertFalse(row_view.note_sighting(jp, "2026-09-22", sample))
            days, _last = row_view.load_journal(jp)
            self.assertEqual(days, {"2026-09-22"})

    def test_note_sighting_never_raises(self):
        self.assertFalse(row_view.note_sighting("/no/such/dir/j.json", "2026-09-22", {}))

    def test_merge_adds_only_missing_days(self):
        counts = {D(2026, 9, 21): 3}
        out, last, total = row_view.merge_journal(counts, "2026-09-21T12:00:00-08:00",
                                                  3, {"2026-09-21", "2026-09-22", "bogus"})
        # 09-21 already rowed 3: preserved, bank untouched.
        self.assertEqual(out[D(2026, 9, 21)], 3)
        self.assertEqual(out[D(2026, 9, 22)], 1)
        self.assertEqual(total, 4)
        self.assertTrue(last.startswith("2026-09-22"))

    def test_merge_does_not_rewind_last(self):
        out, last, total = row_view.merge_journal(
            {}, "2026-09-23T18:00:00-08:00", 0, {"2026-09-22"})
        self.assertTrue(last.startswith("2026-09-23"))
        self.assertEqual(total, 1)


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_frame(payload):
    out = bytearray(b"\x81")
    n = len(payload)
    if n < 126:
        out.append(n)
    elif n < 65536:
        out += b"\x7e" + n.to_bytes(2, "big")
    else:
        out += b"\x7f" + n.to_bytes(8, "big")
    out += payload
    return bytes(out)


def _stats_bytes(connected=True, dist=1500.0, elapsed=400.0):
    return json.dumps({"type": "stats", "connected": connected,
                       "raw": {"distance_m": dist, "elapsed_time_s": elapsed,
                               "stroke_rate": 22},
                       "formatted": {}}).encode("utf-8")


class _FakeWS(threading.Thread):
    """Minimal WS server: handshake, then one text frame pipelined in the
    same segment as the handshake response, then either hold or close."""
    def __init__(self, payload=None, hold_open=False, bad_status=False):
        super().__init__(daemon=True)
        self.payload = payload
        self.hold_open = hold_open
        self.bad_status = bad_status
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]

    @property
    def url(self):
        return "ws://127.0.0.1:%d/obs/ws" % self.port

    def run(self):
        try:
            conn, _addr = self.sock.accept()
        except OSError:
            return
        try:
            conn.settimeout(5)
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                req += chunk
            key = ""
            for line in req.decode("latin-1").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            if self.bad_status:
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                return
            accept = base64.b64encode(
                hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                          "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode("latin-1"))
            if self.payload is not None:
                conn.sendall(_ws_frame(self.payload))
            if self.hold_open:
                time.sleep(10)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass


class _RowsHandler(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):
        if self.path == "/rows.txt":
            data = _RowsHandler.body
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class TestFetchers(unittest.TestCase):
    def _serve_ws(self, **kw):
        srv = _FakeWS(**kw)
        srv.start()
        return srv

    def test_ws_greeting_returns_stats(self):
        srv = self._serve_ws(payload=_stats_bytes())
        msg = row_view.fetch_ws_stats(srv.url, 5)
        self.assertEqual(msg["type"], "stats")
        self.assertEqual(msg["raw"]["distance_m"], 1500.0)

    def test_ws_http_scheme_maps_to_same_socket(self):
        srv = self._serve_ws(payload=_stats_bytes())
        http_url = srv.url.replace("ws://", "http://", 1)
        msg = row_view.fetch_ws_stats(http_url, 5)
        self.assertEqual(msg["raw"]["distance_m"], 1500.0)

    def test_ws_quiet_returns_none_not_raise(self):
        srv = self._serve_ws(payload=None, hold_open=True)
        start = time.monotonic()
        self.assertIsNone(row_view.fetch_ws_stats(srv.url, 2))
        self.assertLess(time.monotonic() - start, 6)

    def test_ws_rejected_handshake_raises(self):
        srv = self._serve_ws(bad_status=True)
        with self.assertRaises(OSError):
            row_view.fetch_ws_stats(srv.url, 5)

    def test_ws_closed_port_raises_fast(self):
        with self.assertRaises(OSError):
            row_view.fetch_ws_stats("ws://127.0.0.1:9/obs/ws", 5)

    def test_ws_garbage_then_close_raises(self):
        srv = self._serve_ws(payload=b"not json")
        with self.assertRaises(OSError):
            row_view.fetch_ws_stats(srv.url, 5)

    def test_http_text_fetch_and_404(self):
        _RowsHandler.body = log_text([("2026-01-01", 2)]).encode("utf-8")
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _RowsHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            text = row_view.fetch_http_text("http://127.0.0.1:%d/rows.txt" % port, 5)
            counts, _last, total = row_view.parse_log(text)
            self.assertEqual(total, 2)
            with self.assertRaises(OSError):
                row_view.fetch_http_text("http://127.0.0.1:%d/missing" % port, 5)
            with self.assertRaises(ValueError):
                row_view.fetch_http_text("ws://127.0.0.1:%d/rows.txt" % port, 5)
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestPollFallback(unittest.TestCase):
    def test_missing_remote_falls_back_to_local(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(log_text([("2026-01-01", 2)]))
            name = fh.name
        try:
            with tempfile.TemporaryDirectory() as tmp:
                jp = os.path.join(tmp, "j.json")
                counts, last, total, ok, err, sighted = row_view.poll_source(
                    "ws", "ws://127.0.0.1:9/obs/ws", 3, name, jp)
                self.assertFalse(ok)
                self.assertTrue(err)
                self.assertFalse(sighted)
                self.assertEqual(total, 2)
                self.assertTrue(last.startswith("2026-01-01"))
        finally:
            os.unlink(name)

    def test_text_url_source_parses(self):
        _RowsHandler.body = log_text([("2026-01-01", 3)]).encode("utf-8")
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _RowsHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                jp = os.path.join(tmp, "j.json")
                counts, _last, total, ok, _err, _s = row_view.poll_source(
                    "text-url", "http://127.0.0.1:%d/rows.txt" % port, 5, None, jp)
                self.assertTrue(ok)
                self.assertEqual(total, 3)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_ws_sighting_journals_today(self):
        srv = _FakeWS(payload=_stats_bytes())
        srv.start()
        with tempfile.TemporaryDirectory() as tmp:
            jp = os.path.join(tmp, "j.json")
            today = datetime.date.today().isoformat()
            counts, last, total, ok, _err, sighted = row_view.poll_source(
                "ws", srv.url, 5, None, jp, today=datetime.date.today())
            self.assertTrue(ok)
            self.assertTrue(sighted)
            self.assertEqual(counts.get(datetime.date.today()), 1)
            self.assertTrue(last.startswith(today))
            days, _last = row_view.load_journal(jp)
            self.assertIn(today, days)

    def test_ws_idle_sights_nothing_but_stays_ok(self):
        srv = _FakeWS(payload=_stats_bytes(connected=False))
        srv.start()
        with tempfile.TemporaryDirectory() as tmp:
            jp = os.path.join(tmp, "j.json")
            counts, _last, _total, ok, _err, sighted = row_view.poll_source(
                "ws", srv.url, 5, None, jp)
            self.assertTrue(ok)
            self.assertFalse(sighted)
            self.assertEqual(counts, {})
            self.assertFalse(os.path.exists(jp))


class TestStaleRender(unittest.TestCase):
    def test_dead_remote_renders_last_known_streak(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(log_text([("2026-01-01", 2), ("2026-01-02", 1)]))
            name = fh.name
        try:
            with tempfile.TemporaryDirectory() as tmp:
                screen = FakeScreen()
                stop = threading.Event()
                threading.Timer(0.3, stop.set).start()
                row_view.run(screen, {
                    "source": "ws://127.0.0.1:9/obs/ws",
                    "path": name,
                    "journal": os.path.join(tmp, "j.json"),
                    "interval": 5,
                }, stop)
                self.assertTrue(screen.frames, "stale remote must keep rendering")
                self.assertTrue(_nonblank(screen.frames[-1]))
        finally:
            os.unlink(name)

    def test_dead_remote_and_no_data_is_not_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            screen = FakeScreen()
            stop = threading.Event()
            threading.Timer(0.3, stop.set).start()
            row_view.run(screen, {
                "source": "ws://127.0.0.1:9/obs/ws",
                "path": "/tmp/does-not-exist-rows.txt",
                "journal": os.path.join(tmp, "j.json"),
                "interval": 5,
            }, stop)
            self.assertTrue(screen.frames)
            self.assertTrue(_nonblank(screen.frames[-1]))


if __name__ == "__main__":
    unittest.main()
