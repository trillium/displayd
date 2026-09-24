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
import pwd
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOOK_PATH = "/hooks/displayd"
MAX_BODY = 1024 * 1024  # 1 MiB; GitHub push payloads are ~10 KiB

BIND = os.environ.get("WEBHOOK_BIND", "100.81.88.113")
PORT = int(os.environ.get("WEBHOOK_PORT", "9898"))
SECRET_FILE = os.environ.get(
    "WEBHOOK_SECRET_FILE", "/root/displayd-webhook-secret")
UPSTREAM_DIR = os.environ.get(
    "UPSTREAM_DIR", "/home/trillium/displayd-upstream")
REMOTE_DIR = os.environ.get("REMOTE_DIR", "/home/trillium/displayd")
PANEL = os.environ.get("PANEL", "100.81.88.113:8980")
DEPLOY_USER = os.environ.get("DEPLOY_USER", "trillium")
DEPLOY_LOG = os.environ.get("DEPLOY_LOG", "/var/log/displayd-webhook.log")

# Mirrors deploy.sh's rsync exclusions; deploy.sh is authoritative.
RSYNC_EXCLUDES = [
    ".git/", ".pi/", "__pycache__/", "*.py[cod]", ".pytest_cache/",
    ".ruff_cache/", ".mypy_cache/", ".venv/", "venv/", ".DS_Store", "._*",
    "DEPLOYED", "policy.json", "*.jsonl", "*_frames/",
]

SECRET = None  # loaded at startup; fail closed when unreadable
STATE_LOCK = threading.Lock()
DEPLOY_LOCK = threading.Lock()
DEPLOYING = {"active": False, "sha": None, "delivery": None}
LAST = {"state": "never", "detail": "no deploy yet", "at": None}


def log_event(obj):
    """Append one JSONL record to the deploy log (best effort) + stderr."""
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    rec.update(obj)
    line = json.dumps(rec)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    try:
        with open(DEPLOY_LOG, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        sys.stderr.write("log write failed: %s\n" % e)


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


def _run(argv, as_tree_user=False, input_bytes=None, timeout=120):
    """Run a command; tree mutations drop to DEPLOY_USER when root."""
    if as_tree_user and os.geteuid() == 0 and DEPLOY_USER != "root":
        argv = ["sudo", "-n", "-u", DEPLOY_USER, "--"] + list(argv)
    return subprocess.run(list(argv), input=input_bytes, capture_output=True,
                          timeout=timeout)


def _http_json(method, url, obj=None, timeout=10):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _write_owned(path, text):
    """Write a file, ensuring it stays owned by DEPLOY_USER (not root)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    if os.geteuid() == 0:
        try:
            pw = pwd.getpwnam(DEPLOY_USER)
            os.chown(tmp, pw.pw_uid, pw.pw_gid)
        except KeyError:
            pass
    os.replace(tmp, path)


def do_deploy(head_sha, delivery):
    """deploy.sh semantics, local variant. Runs in a background thread."""
    t0 = time.time()
    log = lambda step, **kw: log_event(
        {"delivery": delivery, "step": step, "sha": head_sha, **kw})
    with STATE_LOCK:
        LAST.update(state="running", detail=head_sha,
                    at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime()))
    try:
        # 1. Prior view, to re-show after the restart blanks the screen.
        prior = ""
        try:
            prior = _http_json("GET", "http://%s/state" % PANEL).get(
                "renderer") or ""
            log("prior-view", renderer=prior or "(blank)")
        except Exception as e:
            log("prior-view", warning="panel unreachable: %s" % e)

        # 2. Update the upstream clone (as the tree owner), deploy what
        #    origin/main actually points at -- never trust the payload.
        p = _run(["git", "-C", UPSTREAM_DIR, "fetch", "origin", "main"],
                 as_tree_user=True, timeout=120)
        if p.returncode != 0:
            raise RuntimeError("git fetch failed: %s" % p.stderr.decode()[:300])
        actual = _run(["git", "-C", UPSTREAM_DIR, "rev-parse",
                       "origin/main"], as_tree_user=True).stdout.decode().strip()
        if len(actual) != 40:
            raise RuntimeError("bad origin/main sha: %r" % actual)
        log("fetched", actual=actual, claimed=head_sha,
            match=(actual == head_sha))
        p = _run(["git", "-C", UPSTREAM_DIR, "reset", "--hard",
                  "origin/main"], as_tree_user=True, timeout=120)
        if p.returncode != 0:
            raise RuntimeError("git reset failed: %s" % p.stderr.decode()[:300])

        # 3. Sync the live tree (same exclusions as deploy.sh, no --delete:
        #    host-local files must survive).
        cmd = ["rsync", "-a"]
        for pat in RSYNC_EXCLUDES:
            cmd += ["--exclude", pat]
        cmd += [UPSTREAM_DIR.rstrip("/") + "/", REMOTE_DIR.rstrip("/") + "/"]
        p = _run(cmd, as_tree_user=True, timeout=300)
        if p.returncode != 0:
            raise RuntimeError("rsync failed: %s" % p.stderr.decode()[:300])
        log("synced", sha=actual)

        # 4. Restart the daemon.
        if os.geteuid() == 0:
            p = _run(["systemctl", "restart", "displayd"], timeout=60)
        else:
            p = _run(["sudo", "-n", "systemctl", "restart", "displayd"],
                     timeout=60)
        if p.returncode != 0:
            raise RuntimeError("restart failed: %s" % p.stderr.decode()[:300])
        log("restarted")

        # 5. Wait for health, re-show the prior view, prove the build.
        healthy = False
        for _ in range(30):
            try:
                if _http_json("GET", "http://%s/health" % PANEL,
                              timeout=5).get("ok"):
                    healthy = True
                    break
            except Exception:
                time.sleep(1)
        if not healthy:
            raise RuntimeError("panel never became healthy")
        if prior:
            try:
                _http_json("POST", "http://%s/show" % PANEL,
                           {"renderer": prior})
                log("re-showed", renderer=prior)
            except Exception as e:
                log("re-showed", warning="could not re-show: %s" % e)
        try:
            _http_json("POST", "http://%s/reload" % PANEL, {"sha": actual})
            log("reload-proof")
        except Exception as e:
            log("reload-proof", warning="proof failed: %s" % e)

        # 6. Delivery stamp, host-side only (never committed).
        stamp = json.dumps({
            "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sha": actual, "deployer": "webhook"})
        _write_owned(os.path.join(REMOTE_DIR, "DEPLOYED"), stamp)
        _write_owned(os.path.join(REMOTE_DIR, ".displayd-release"),
                     actual + "\n")
        log("stamped", stamp=stamp)

        # 7. Verify: stamp serves this SHA, screen shows content.
        got = _http_json("GET", "http://%s/deploy" % PANEL)
        if actual not in json.dumps(got):
            raise RuntimeError("/deploy does not show %s: %r"
                               % (actual, got))
        state = _http_json("GET", "http://%s/state" % PANEL)
        if not state.get("renderer"):
            raise RuntimeError("panel shows nothing after deploy")
        log("verified", renderer=state.get("renderer"),
            elapsed_s=round(time.time() - t0, 1))
        with STATE_LOCK:
            LAST.update(state="ok", detail=actual,
                        at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()))
    except Exception as e:
        log("FAILED", error=str(e)[:500],
            elapsed_s=round(time.time() - t0, 1))
        with STATE_LOCK:
            LAST.update(state="failed", detail=str(e)[:300],
                        at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()))
    finally:
        with DEPLOY_LOCK:
            DEPLOYING.update(active=False, sha=None, delivery=None)


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
