#!/usr/bin/env python3
"""displayd GitHub webhook receiver (stdlib only).

Listens tailnet-only on lnx-server for POST /hooks/displayd from GitHub
(delivered via the vps01 funnel forwarder). Verifies the GitHub HMAC
signature, then performs deploy.sh semantics locally:

  fetch + reset the upstream clone, sync the live tree, restart the
  displayd daemon, re-show the prior view, prove the build with /reload,
  write the DEPLOYED stamp, verify health + stamp.

Runs as its own systemd unit (hooks/displayd-webhook.service), separate
from the displayd daemon itself, so the daemon restart mid-deploy never
kills an in-flight deploy.

Only POST /hooks/displayd is served (plus a local /health status read);
everything else is 404. The shared secret is never logged or printed.

Configuration (environment overrides):
  WEBHOOK_BIND         bind address (default 100.81.88.113 -- tailnet only;
                       binding 0.0.0.0 is refused outright)
  WEBHOOK_PORT         listen port (default 9898)
  WEBHOOK_SECRET_FILE  shared secret file (default /root/displayd-webhook-secret)
  UPSTREAM_DIR         git clone updated on each deploy
                       (default /home/trillium/displayd-upstream)
  REMOTE_DIR           live daemon tree (default /home/trillium/displayd)
  PANEL                panel API host:port (default 100.81.88.113:8980)
  DEPLOY_USER          unix user owning the trees (default trillium)
  DEPLOY_LOG           JSONL deploy log (default /var/log/displayd-webhook.log)
"""

import hashlib
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import webhook_deploy as _deploy

HOOK_PATH = "/hooks/displayd"
MAX_BODY = 1024 * 1024  # 1 MiB; GitHub push payloads are ~10 KiB

BIND = os.environ.get("WEBHOOK_BIND", "100.81.88.113")
PORT = int(os.environ.get("WEBHOOK_PORT", "9898"))
SECRET_FILE = os.environ.get(
    "WEBHOOK_SECRET_FILE", "/root/displayd-webhook-secret")
SECRET = None  # loaded at startup; fail closed when unreadable

# Deploy pipeline lives in webhook_deploy.py; shared config and state are
# owned there. Re-exported for backwards compatibility.
UPSTREAM_DIR = _deploy.UPSTREAM_DIR
REMOTE_DIR = _deploy.REMOTE_DIR
PANEL = _deploy.PANEL
DEPLOY_USER = _deploy.DEPLOY_USER
DEPLOY_LOG = _deploy.DEPLOY_LOG
RSYNC_EXCLUDES = _deploy.RSYNC_EXCLUDES
STATE_LOCK = _deploy.STATE_LOCK
DEPLOY_LOCK = _deploy.DEPLOY_LOCK
DEPLOYING = _deploy.DEPLOYING
LAST = _deploy.LAST
log_event = _deploy.log_event
_run = _deploy._run
_http_json = _deploy._http_json
_write_owned = _deploy._write_owned
do_deploy = _deploy.do_deploy


def load_secret(path=SECRET_FILE):
    """Read the shared secret. Returns bytes; raises on any problem."""
    with open(path, "rb") as f:
        secret = f.read().strip()
    if len(secret) < 16:
        raise ValueError("secret file %s too short" % path)
    return secret


def verify_signature(secret, body, sig_header):
    """Check GitHub's X-Hub-Signature-256 header (constant-time)."""
    if not secret or not sig_header:
        return False
    if not sig_header.startswith("sha256="):
        return False
    try:
        theirs = bytes.fromhex(sig_header[len("sha256="):])
    except ValueError:
        return False
    ours = hmac.new(secret, body, hashlib.sha256).digest()
    return hmac.compare_digest(ours, theirs)


def classify_event(event, payload):
    """Route a webhook delivery.

    Returns (action, detail) with action in {"pong", "deploy", "skip"}.
    """
    if event == "ping":
        return ("pong", "ping acked, no deploy")
    if event != "push":
        return ("skip", "ignoring event %r" % (event,))
    if payload.get("deleted"):
        return ("skip", "branch deleted, nothing to deploy")
    ref = payload.get("ref", "")
    if ref != "refs/heads/main":
        return ("skip", "ref %r is not main" % (ref,))
    sha = payload.get("after") or ""
    if (len(sha) != 40
            or any(c not in "0123456789abcdef" for c in sha)):
        return ("skip", "bad head sha")
    return ("deploy", sha)


class Handler(BaseHTTPRequestHandler):
    server_version = "displayd-webhook/1"

    def log_message(self, fmt, *args):
        sys.stderr.write("http: " + fmt % args + "\n")

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/health":
            with STATE_LOCK:
                last = dict(LAST)
            self._send(200, {"ok": True, "last": last})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path != HOOK_PATH:
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(413 if length > MAX_BODY else 400,
                       {"error": "bad content length"})
            return
        body = self.rfile.read(length)
        delivery = self.headers.get("X-GitHub-Delivery", "?")
        if not verify_signature(SECRET, body,
                                self.headers.get("X-Hub-Signature-256")):
            log_event({"delivery": delivery, "step": "rejected",
                       "reason": "bad signature"})
            self._send(403, {"error": "bad signature"})
            return
        try:
            payload = json.loads(body.decode())
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "bad json"})
            return
        event = self.headers.get("X-GitHub-Event")
        action, detail = classify_event(event, payload)
        if action != "deploy":
            self._send(200, {"ok": True, "action": action, "detail": detail})
            return
        with DEPLOY_LOCK:
            if DEPLOYING["active"]:
                self._send(202, {"ok": True, "action": "already-deploying",
                                 "sha": DEPLOYING["sha"]})
                return
            DEPLOYING.update(active=True, sha=detail, delivery=delivery)
        t = threading.Thread(target=do_deploy, args=(detail, delivery),
                             daemon=True)
        t.start()
        log_event({"delivery": delivery, "step": "accepted", "sha": detail})
        self._send(202, {"ok": True, "action": "deploying", "sha": detail})


def main():
    global SECRET
    if BIND in ("0.0.0.0", "::", ""):
        sys.stderr.write("refusing to bind %r: tailnet-only\n" % BIND)
        sys.exit(2)
    try:
        SECRET = load_secret()
    except (OSError, ValueError) as e:
        sys.stderr.write("cannot load secret %s: %s (failing closed)\n"
                         % (SECRET_FILE, e))
        sys.exit(2)
    if not os.path.isdir(UPSTREAM_DIR):
        sys.stderr.write("upstream clone missing: %s\n" % UPSTREAM_DIR)
        sys.exit(2)
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    httpd.daemon_threads = True
    sys.stderr.write("webhook receiver on %s:%d -> %s\n"
                     % (BIND, PORT, REMOTE_DIR))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
