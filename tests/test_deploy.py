"""Deploy-stamp tests: read_deploy_stamp, daemon surface, HTTP endpoint.

deploy.sh writes a JSON stamp (UTC date + commit SHA + deployer) to a
host-side DEPLOYED file next to the daemon. The daemon only reads it:
GET /deploy and GET /state's "deploy" key surface the stamp, and the
control page renders it. A missing or corrupt stamp is "never recorded",
never an error.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import inspect
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import displayd

SHA = "bf117d324b8425954b81a1bf4845dc6cfb2e36af"


class TestReadDeployStamp(unittest.TestCase):
    def test_missing_file_is_never_recorded(self):
        self.assertEqual(displayd.read_deploy_stamp("/nonexistent/DEPLOYED"),
                         {"deployed": False})

    def test_valid_stamp(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump({"date": "2026-09-22T21:30:00Z", "sha": SHA,
                       "deployer": "trillium"}, fh)
            path = fh.name
        try:
            got = displayd.read_deploy_stamp(path)
        finally:
            os.unlink(path)
        self.assertTrue(got["deployed"])
        self.assertEqual(got["sha"], SHA)
        self.assertEqual(got["date"], "2026-09-22T21:30:00Z")
        self.assertEqual(got["deployer"], "trillium")

    def test_corrupt_file_is_never_recorded(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            fh.write("not json {{{")
            path = fh.name
        try:
            got = displayd.read_deploy_stamp(path)
        finally:
            os.unlink(path)
        self.assertEqual(got, {"deployed": False})

    def test_stamp_without_sha_is_never_recorded(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump({"date": "2026-09-22T21:30:00Z"}, fh)
            path = fh.name
        try:
            got = displayd.read_deploy_stamp(path)
        finally:
            os.unlink(path)
        self.assertEqual(got, {"deployed": False})

    def test_env_override_selects_stamp_path(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump({"date": "2026-09-22T21:30:00Z", "sha": SHA,
                       "deployer": "trillium"}, fh)
            path = fh.name
        old = os.environ.get("DISPLAYD_DEPLOY_STAMP")
        os.environ["DISPLAYD_DEPLOY_STAMP"] = path
        try:
            self.assertEqual(displayd.deploy_stamp_path(), path)
            self.assertTrue(displayd.read_deploy_stamp()["deployed"])
        finally:
            if old is None:
                os.environ.pop("DISPLAYD_DEPLOY_STAMP", None)
            else:
                os.environ["DISPLAYD_DEPLOY_STAMP"] = old
            os.unlink(path)


class TestDeploySurface(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("DISPLAYD_FAKE_FB")
        os.environ["DISPLAYD_FAKE_FB"] = "1"
        self._stamp = os.environ.get("DISPLAYD_DEPLOY_STAMP")
        os.environ.pop("DISPLAYD_DEPLOY_STAMP", None)
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
        if self._stamp is None:
            os.environ.pop("DISPLAYD_DEPLOY_STAMP", None)
        else:
            os.environ["DISPLAYD_DEPLOY_STAMP"] = self._stamp

    def test_state_carries_deploy_key(self):
        # No stamp file in the temp worktree: never recorded, not an error.
        state = self.daemon.state()
        self.assertIn("deploy", state)
        self.assertEqual(state["deploy"], {"deployed": False})

    def test_deploy_info_reflects_stamp_file(self):
        path = os.path.join(self.tmp.name, "DEPLOYED")
        with open(path, "w") as fh:
            json.dump({"date": "2026-09-22T21:30:00Z", "sha": SHA,
                       "deployer": "trillium"}, fh)
        os.environ["DISPLAYD_DEPLOY_STAMP"] = path
        info = self.daemon.deploy_info()
        self.assertTrue(info["deployed"])
        self.assertEqual(info["sha"], SHA)
        self.assertEqual(self.daemon.state()["deploy"]["sha"], SHA)

    def test_handler_routes_deploy_to_json(self):
        src = inspect.getsource(displayd.Handler.do_GET)
        self.assertIn('"/deploy"', src)
        self.assertIn("deploy_info", src)

    def test_control_page_shows_deploy_stamp(self):
        page = displayd.CONTROL_PAGE
        for token in ("dep-when", "dep-sha", "dep-who", "s.deploy"):
            self.assertIn(token, page, "page never shows %s" % token)

    def test_default_bind_unchanged(self):
        self.assertEqual(displayd.BIND, "127.0.0.1",
                         "default bind must stay loopback: the API has no auth")


if __name__ == "__main__":
    unittest.main()
