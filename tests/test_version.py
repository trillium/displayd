"""Version tests: single source of truth, API surface, startup log.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import inspect
import json
import os
import re
import sys
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib import request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd

HOST = "127.0.0.1"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
                       r"(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")


class TestVersionSource(unittest.TestCase):
    def test_app_version_is_plain_semver(self):
        self.assertRegex(displayd.APP_VERSION, SEMVER_RE,
                         "APP_VERSION must be plain semver")
        self.assertNotIn("+fork", displayd.APP_VERSION)
        self.assertTrue(displayd.APP_VERSION.startswith("0."),
                        "first release stays 0.x: no 1.0 stability promise yet")

    def test_no_second_version_copy(self):
        # APP_VERSION is the single source of truth: pyproject carries no
        # [project] version that could drift (it holds only tool config).
        with open(os.path.join(os.path.dirname(__file__), os.pardir,
                               "pyproject.toml")) as fh:
            pyproject = fh.read()
        self.assertNotIn(displayd.APP_VERSION, pyproject,
                         "version string duplicated into pyproject.toml")
        self.assertNotIn("[project]", pyproject)


class TestVersionSurface(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._restore_env)
        self.daemon = displayd.DisplayDaemon(
            policy_path=os.path.join(self.tmp.name, "policy.json"))

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("DISPLAYD_FAKE_FB", None)
        else:
            os.environ["DISPLAYD_FAKE_FB"] = self._env

    def test_state_carries_version(self):
        state = self.daemon.state()
        self.assertEqual(state["version"], displayd.APP_VERSION)

    def test_handler_routes_version(self):
        src = inspect.getsource(displayd.Handler.do_GET)
        self.assertIn('"/version"', src)


class TestVersionHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        cls.old_daemon = displayd.DAEMON
        cls.tmp = tempfile.TemporaryDirectory()
        displayd.DAEMON = displayd.DisplayDaemon(
            policy_path=os.path.join(cls.tmp.name, "policy.json"))
        cls.server = ThreadingHTTPServer((HOST, 0), displayd.Handler)
        cls.server.daemon_threads = True
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        displayd.DAEMON = cls.old_daemon
        cls.tmp.cleanup()
        os.environ.pop("DISPLAYD_FAKE_FB", None)

    def test_get_version(self):
        with request.urlopen("http://%s:%d/version" % (HOST, self.port),
                             timeout=10) as resp:
            body = json.load(resp)
        self.assertEqual(body, {"version": displayd.APP_VERSION})

    def test_state_version_matches(self):
        with request.urlopen("http://%s:%d/state" % (HOST, self.port),
                             timeout=10) as resp:
            body = json.load(resp)
        self.assertEqual(body["version"], displayd.APP_VERSION)

    def test_startup_log_says_version(self):
        src = inspect.getsource(displayd.main)
        self.assertIn("APP_VERSION", src)
