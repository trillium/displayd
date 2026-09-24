"""Webhook receiver / forwarder tests (stdlib + unittest only).

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import hashlib
import hmac
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                os.pardir, "hooks"))

import webhook_receiver as wr
import funnel_forwarder as ff


class TestSignature(unittest.TestCase):
    def test_valid_signature(self):
        secret = b"test-secret-12345678"
        body = b'{"ref":"refs/heads/main"}'
        sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        self.assertTrue(wr.verify_signature(secret, body, sig))

    def test_wrong_secret_rejected(self):
        body = b"{}"
        sig = "sha256=" + hmac.new(b"right-secret-12345", body,
                                   hashlib.sha256).hexdigest()
        self.assertFalse(wr.verify_signature(b"wrong-secret-12345", body, sig))

    def test_tampered_body_rejected(self):
        secret = b"test-secret-12345678"
        sig = "sha256=" + hmac.new(secret, b"{}", hashlib.sha256).hexdigest()
        self.assertFalse(wr.verify_signature(secret, b'{"evil":1}', sig))

    def test_missing_and_malformed_headers_rejected(self):
        secret = b"test-secret-12345678"
        for bad in (None, "", "sha256=", "sha256=zzz", "md5=abcd",
                    "SHA256=" + "ab" * 32):
            self.assertFalse(wr.verify_signature(secret, b"{}", bad),
                             "header %r must be rejected" % (bad,))


class TestClassifyEvent(unittest.TestCase):
    def test_ping_pongs_without_deploy(self):
        self.assertEqual(wr.classify_event("ping", {})[0], "pong")

    def test_push_to_main_deploys(self):
        action, detail = wr.classify_event(
            "push", {"ref": "refs/heads/main",
                     "after": "a" * 40, "deleted": False})
        self.assertEqual(action, "deploy")
        self.assertEqual(detail, "a" * 40)

    def test_push_to_other_branch_skips(self):
        action, _ = wr.classify_event(
            "push", {"ref": "refs/heads/feature", "after": "b" * 40})
        self.assertEqual(action, "skip")

    def test_branch_delete_skips(self):
        action, _ = wr.classify_event(
            "push", {"ref": "refs/heads/main", "deleted": True,
                     "after": "0" * 40})
        self.assertEqual(action, "skip")

    def test_bad_sha_skips(self):
        for bad in ("", "short", "z" * 40, None):
            action, _ = wr.classify_event(
                "push", {"ref": "refs/heads/main", "after": bad})
            self.assertEqual(action, "skip", "sha %r must skip" % (bad,))

    def test_non_push_events_skip(self):
        for event in ("issues", "pull_request", None, ""):
            self.assertEqual(wr.classify_event(event, {})[0], "skip")


class TestForwarderAllowlist(unittest.TestCase):
    def test_only_hook_post_forwarded(self):
        self.assertTrue(ff.forward_allowed("POST", "/hooks/displayd"))

    def test_everything_else_blocked(self):
        for method, path in (("GET", "/hooks/displayd"), ("POST", "/"),
                             ("POST", "/hooks/other"), ("GET", "/"),
                             ("PUT", "/hooks/displayd"),
                             ("POST", "/hooks/displayd/extra")):
            self.assertFalse(ff.forward_allowed(method, path),
                             "%s %s must not forward" % (method, path))


if __name__ == "__main__":
    unittest.main()
