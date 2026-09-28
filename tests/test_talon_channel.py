"""Talon file-channel tests (bridges/talon_channel.py).

Atomic request write, id-matched response wait, timeout and mismatch
behaviour -- all with a fake Talon responder thread, no Talon needed.

Run from the repo root:  python3 -m unittest tests.test_talon_channel -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir,
                                "bridges"))

import talon_channel as ch


def responder(directory, req, resp, answer, delay=0.05, stop=None):
    """Fake Talon tick: answer one request id with `answer`."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return
        path = os.path.join(directory, req)
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    doc = json.load(fh)
            except Exception:
                doc = None
            try:
                os.unlink(path)
            except Exception:
                pass
            if isinstance(doc, dict):
                body = dict(answer)
                body["id"] = doc.get("id")
                tmp = os.path.join(directory, "%s.tmp" % resp)
                with open(tmp, "w") as fh:
                    json.dump(body, fh)
                os.replace(tmp, os.path.join(directory, resp))
                return
        time.sleep(0.01)


class ExchangeTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = threading.Thread(
                target=responder,
                args=(tmp, "r.json", "s.json", {"ok": True}))
            worker.start()
            resp = ch.exchange(tmp, "r.json", "s.json",
                               {"id": 7, "ts": time.time()})
            worker.join(timeout=10)
            self.assertEqual(resp, {"ok": True, "id": 7})

    def test_timeout_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            t0 = time.monotonic()
            self.assertIsNone(ch.exchange(tmp, "r.json", "s.json",
                                          {"id": 1, "ts": time.time()},
                                          timeout=0.2))
            self.assertLess(time.monotonic() - t0, 2.0)

    def test_wrong_id_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "s.json"), "w") as fh:
                json.dump({"id": "nope", "ok": True}, fh)
            self.assertIsNone(ch.exchange(tmp, "r.json", "s.json",
                                          {"id": 1, "ts": time.time()},
                                          timeout=0.2))

    def test_cleanup_unlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.json", "b.json"):
                with open(os.path.join(tmp, name), "w") as fh:
                    fh.write("{}")
            ch.cleanup(tmp, "a.json", "b.json", "missing.json")
            self.assertEqual(os.listdir(tmp), [])

    def test_garbage_never_raises(self):
        self.assertIsNone(ch.exchange("/nonexistent-dir-xyz", "r", "s",
                                      {"id": 1}, timeout=0.1))
        self.assertIsNone(ch.exchange(tempfile.gettempdir(), "r", "s",
                                      "not-a-dict", timeout=0.1))
        ch.cleanup("/nonexistent-dir-xyz", "x")
        self.assertTrue(ch.comm_dir("/fixed").endswith("/fixed"))


if __name__ == "__main__":
    unittest.main()
