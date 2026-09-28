#!/usr/bin/env python3
"""Capture a genuine snapshot of every advertised displayd view.

Runs a headless daemon (DISPLAYD_FAKE_FB=1: the in-memory framebuffer the
daemon itself documents as behaving "identically minus photons"), presents
each renderer the daemon actually advertises via GET /renderers, and saves
the real presented frames as docs/screenshots/<view>.png at the panel's
native 1920x1080.

Usage (from the repo root):

    python3 tools/capture_screenshots.py

The capture needs DejaVu fonts where the daemon looks for them
(/usr/share/fonts, see Screen.font_path): without them, text-heavy views
fall back to PIL's bitmap font and the shots misrepresent the panel.
Linux hosts (including the panel host) already have them; on macOS run
the same script inside a container instead:

    SHA=$(git rev-parse HEAD); docker run --rm -v "$PWD:/shot" -w /shot \
      -e DISPLAYD_SHOT_SHA="$SHA" python:3.13-slim bash -c \
      "apt-get update -qq && apt-get install -y -qq fonts-dejavu-core >/dev/null 2>&1 && pip install -q pillow && python3 tools/capture_screenshots.py"

DISPLAYD_SHOT_SHA seeds the reload view's SHA param (defaults to this
checkout's HEAD, else a valid-shape placeholder); the shots in this
directory were taken with the container command above.

The script spawns its own daemon on an ephemeral port, so it never touches
the live panel. Policy, feedback, and deploy-stamp paths are pointed at a
temporary directory, so the run leaves no state behind in the repo. Views
that need content to be meaningful are seeded through the real feed API
(chat messages, one stream frame) -- every PNG is still a genuine
/dev/snapshot-style frame, never a mockup.

Seeded content and special params (required PARAMS have no defaults):

  chat    two messages pushed via POST /feed/chat/message first
  stream  one frame pushed via POST /feed/stream/frame (the clock capture
          just taken, so the frame is real panel output through the feed)
  image   shown with path=<clock capture> (needs a real file; captured in
          a second pass once clock.png exists)
  notice  {title, body, severity info}
  qr      {data: the repo URL, caption}
  reload  {sha: this checkout's HEAD} (must be a full 40-char SHA)
  text    {text: a fixed pangram-ish sample}

Everything else is shown with default params, i.e. exactly what an
operator gets from POST /show {"renderer": "<view>"}.
"""

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from urllib import request
from urllib.error import HTTPError

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "docs", "screenshots")

SETTLE_SECONDS = 2.5
SNAPSHOT_TIMEOUT = 20


def call(method, base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype:
                return resp.status, json.loads(body.decode())
            return resp.status, body
    except HTTPError as exc:
        return exc.code, exc.read()


def wait_healthy(base, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = call("GET", base, "/health")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError("daemon never became healthy at %s" % base)


def wait_snapshot(base, timeout=SNAPSHOT_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = call("GET", base, "/snapshot")
        if status == 200 and body:
            return body
        time.sleep(0.3)
    raise RuntimeError("no snapshot frame presented in time")


def head_sha():
    hint = os.environ.get("DISPLAYD_SHOT_SHA", "")
    if len(hint) == 40 and all(c in "0123456789abcdefABCDEF"
                                for c in hint):
        return hint.lower()
    try:
        out = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=15)
        sha = out.stdout.strip()
        if len(sha) == 40 and all(c in "0123456789abcdefABCDEF"
                                  for c in sha):
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    return "a" * 40  # valid shape, clearly a placeholder


def main():
    tmp = tempfile.mkdtemp(prefix="displayd-shots-")
    env = dict(os.environ)
    env["DISPLAYD_FAKE_FB"] = "1"
    env["DISPLAYD_POLICY"] = os.path.join(tmp, "policy.json")
    env["DISPLAYD_FEEDBACK"] = os.path.join(tmp, "feedback.jsonl")
    env["DISPLAYD_DEPLOY_STAMP"] = os.path.join(tmp, "DEPLOYED")

    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = "http://127.0.0.1:%d" % port
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "displayd.py"),
         "--bind", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    try:
        wait_healthy(base)
        status, advertised = call("GET", base, "/renderers")
        assert status == 200, advertised
        names = [r["name"] for r in advertised["renderers"]
                 if "module" in r or r.get("name")]
        # Only capturable renderers: entries that loaded (have params).
        names = [r["name"] for r in advertised["renderers"]
                 if "params" in r]
        status, version_body = call("GET", base, "/version")
        version = version_body["version"] if status == 200 else "unknown"

        sha = head_sha()
        special = {
            "notice": {"title": "Screenshot catalog",
                       "body": "Regenerated by\ntools/capture_screenshots.py",
                       "severity": "info"},
            "qr": {"data": "https://github.com/trillium/displayd",
                   "caption": "trillium/displayd"},
            "reload": {"sha": sha},
            "text": {"text": "displayd screenshot catalog\n"
                             "0123456789 AaBbCcZz"},
        }

        os.makedirs(OUT_DIR, exist_ok=True)

        # Seed chat through the real feed path before its capture.
        for author, text in (("captain", "screenshot pass starting"),
                             ("firstmate", "all views will be captured")):
            status, body = call("POST", base, "/feed/chat/message",
                                {"author": author, "text": text})
            assert status == 200, body

        def capture(name, params):
            status, body = call("POST", base, "/show",
                                {"renderer": name, "params": params})
            assert status == 200, (name, body)
            time.sleep(SETTLE_SECONDS)
            return wait_snapshot(base)

        # Clock first: its real frame seeds the stream feed and the
        # image view below (both need genuine panel output as input).
        order = sorted(names)
        order.remove("clock")
        order.insert(0, "clock")
        frames = {}
        for name in order:
            if name in ("image", "stream"):
                continue  # second pass, needs clock.png on disk
            frames[name] = capture(name, special.get(name, {}))

        from PIL import Image
        total = 0
        for name, png in frames.items():
            img = Image.open(io.BytesIO(png))
            path = os.path.join(OUT_DIR, name + ".png")
            img.save(path, format="PNG", optimize=True)
            total += os.path.getsize(path)

        clock_path = os.path.join(OUT_DIR, "clock.png")
        with open(clock_path, "rb") as fh:
            clock_png = fh.read()
        status, body = call(
            "POST", base, "/feed/stream/frame",
            {"data": base64.b64encode(clock_png).decode()})
        assert status == 200, body
        for name in ("stream", "image"):
            if name not in names:
                continue
            params = ({"path": clock_path} if name == "image"
                      else special.get(name, {}))
            png = capture(name, params)
            img = Image.open(io.BytesIO(png))
            path = os.path.join(OUT_DIR, name + ".png")
            img.save(path, format="PNG", optimize=True)

        with open(os.path.join(OUT_DIR, "VERSION"), "w") as fh:
            fh.write(version + "\n")
        saved = sorted(f for f in os.listdir(OUT_DIR) if f.endswith(".png"))
        total = sum(os.path.getsize(os.path.join(OUT_DIR, f)) for f in saved)
        missing = sorted(set(names) - {f[:-4] for f in saved})
        print("displayd %s: %d views -> %s (%d bytes total)"
              % (version, len(saved), OUT_DIR, total))
        for name in saved:
            print("  %s" % name)
        if missing:
            print("NOT CAPTURED: %s" % ", ".join(missing))
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        if proc.returncode not in (0, -15):
            print(out)


if __name__ == "__main__":
    main()
