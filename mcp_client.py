"""mcp_client - displayd HTTP client for the MCP server.

Single concept: reach displayd's HTTP API (or fail loudly). Owns the
``DISPLAYD_URL`` / ``DISPLAYD_TIMEOUT`` environment config, the
``DisplayUnreachable`` vs ``DisplayError`` distinction, and the
``api_get`` / ``api_post`` helpers the rest of the server builds on.
"""

import json
import os
import urllib.error
import urllib.request

DISPLAYD_URL = os.environ.get("DISPLAYD_URL", "http://127.0.0.1:8980").rstrip("/")
try:
    TIMEOUT = float(os.environ.get("DISPLAYD_TIMEOUT", "10"))
except ValueError:
    TIMEOUT = 10.0


class DisplayUnreachable(Exception):
    """The panel/daemon could not be reached at all (TCP refused, DNS,
    timeout). Distinct from an HTTP error *from* displayd."""


class DisplayError(Exception):
    """displayd answered with a non-2xx status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _request(method, path, body=None, raw=False):
    url = DISPLAYD_URL + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode() or "{}")
            message = detail.get("error", str(detail))
        except ValueError:
            message = "HTTP %d" % exc.code
        raise DisplayError(exc.code, message)
    except (urllib.error.URLError, ConnectionError, TimeoutError,
            OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise DisplayUnreachable("%s: %s" % (url, reason))
    if raw or "image/" in ctype:
        return payload
    if not payload:
        return {}
    try:
        return json.loads(payload.decode())
    except ValueError:
        raise DisplayError(500, "displayd returned non-JSON")


def api_get(path, raw=False):
    return _request("GET", path, raw=raw)


def api_post(path, body=None):
    return _request("POST", path, body=body if body is not None else {})
