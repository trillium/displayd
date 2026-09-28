#!/usr/bin/env python3
"""Deploy pipeline for the displayd GitHub webhook receiver (stdlib only).

Single concept: deploy.sh semantics as a local function -- update the
upstream clone, sync the live tree, restart the daemon, re-show the prior
view, prove the build with /reload, write the DEPLOYED stamp, verify.

The HTTP/signature half lives in webhook_receiver.py, which owns the
server state (DEPLOYING/LAST live there and are shared from here).
"""

import json
import os
import pwd
import subprocess
import sys
import threading
import time
import urllib.request

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
