#!/usr/bin/env python3
"""Funnel forwarder on vps01 (stdlib only).

Sits behind the existing Tailscale funnel inlet
(https://vps01.hippo-tilapia.ts.net -> 127.0.0.1:8090; funnel config is
untouched) and forwards ONLY POST /hooks/displayd to the lnx webhook
receiver over the tailnet. Everything else gets a bare 404 and never
touches the interior.

Signature verification happens on the receiver, not here; this process
holds no secrets.

Configuration (environment overrides):
  FUNNEL_LISTEN  bind address (default 127.0.0.1 -- funnel target is local)
  FUNNEL_PORT    listen port (default 8090, the existing funnel target)
  TARGET         receiver base URL (default http://100.81.88.113:9898)
"""

import http.client
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOOK_PATH = "/hooks/displayd"
MAX_BODY = 2 * 1024 * 1024

LISTEN = os.environ.get("FUNNEL_LISTEN", "127.0.0.1")
PORT = int(os.environ.get("FUNNEL_PORT", "8090"))
TARGET = os.environ.get("TARGET", "http://100.81.88.113:9898")

# The only headers worth forwarding; hop-by-hop headers are dropped.
FORWARD_HEADERS = ("content-type", "content-length", "x-hub-signature-256",
                   "x-github-event", "x-github-delivery", "user-agent")


def forward_allowed(method, path):
    """Single funnel route: nothing else leaves this box."""
    return method == "POST" and path == HOOK_PATH


class Handler(BaseHTTPRequestHandler):
    server_version = "displayd-funnel-fwd/1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    time.gmtime()),
                                      fmt % args))

    def _send(self, code, body=b"not found"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(400 if length <= 0 else 413, b"bad content length")
            return
        body = self.rfile.read(length)
        fwd = {k: self.headers[k] for k in FORWARD_HEADERS
               if k in self.headers}
        try:
            t = urllib.parse.urlparse(TARGET)
            conn = http.client.HTTPConnection(t.hostname, t.port, timeout=8)
            conn.request("POST", HOOK_PATH, body=body, headers=fwd)
            resp = conn.getresponse()
            out = resp.read(1 * 1024 * 1024)
            self.send_response(resp.status)
            ctype = resp.getheader("Content-Type", "application/json")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            self.log_message("forward %s -> %s", HOOK_PATH, resp.status)
        except Exception as e:
            self.log_message("upstream error: %s", e)
            self._send(502, b"bad gateway")

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        if not forward_allowed("POST", self.path):
            self._send(404)
            return
        self._forward()

    def do_GET(self):  # noqa: N802
        self._send(404)

    def do_PUT(self):  # noqa: N802
        self._send(404)

    def do_DELETE(self):  # noqa: N802
        self._send(404)


def main():
    httpd = ThreadingHTTPServer((LISTEN, PORT), Handler)
    httpd.daemon_threads = True
    sys.stderr.write("funnel forwarder %s:%d -> %s%s\n"
                     % (LISTEN, PORT, TARGET, HOOK_PATH))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
