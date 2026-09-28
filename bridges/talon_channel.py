"""File-protocol client for the displayd <-> Talon channel (Mac-side).

Both Mac bridges speak to Talon through tmp ``displayd-apps-<uid>/``
files (the Talon side lives in
``~/.talon/.../core/displayd_apps/displayd_apps.py``): the bridge
atomically writes ``<verb>_request.json``, Talon's 150ms cron tick
consumes it (unlink on read, so a retried fetch never double-fires)
and atomically writes ``<verb>_response.json``, which the bridge waits
for HERE -- never on Talon's serial main thread (brain-15l95: no copy
of the rpc_client blocking primitive is added or used).

One verb, one request/response filename pair; TTL-expiry refuses
stale work instead of firing late. Stdlib only, no repo imports, so
both durable bridge copies stay self-contained. Never raises out of
:func:`exchange` (None on any failure: the caller degrades, never a
half-command).
"""

import json
import os
import tempfile
import time

REQUEST_TTL = 10.0  # requests older than this never run (Talon-side TTL)
POLL_STEP = 0.05  # response poll granularity inside exchange()


def comm_dir(path=None):
    """Agree with the Talon side on the file-protocol directory."""
    if path:
        return path
    suffix = "-%s" % os.getuid() if hasattr(os, "getuid") else ""
    return os.path.join(tempfile.gettempdir(), "displayd-apps%s" % suffix)


def _atomic_write(path, doc):
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


def exchange(directory, request_name, response_name, body,
             timeout=4.0, now=None):
    """Write one request, wait for its answer. Returns the response
    doc, or None on timeout/absence/mismatch -- files are consumed
    either way so nothing replays. `body` must carry "id" and "ts".
    Never raises."""
    try:
        now = time.time() if now is None else float(now)
        req_path = os.path.join(directory, request_name)
        resp_path = os.path.join(directory, response_name)
        try:
            os.unlink(resp_path)  # stale answers never match a new id
        except FileNotFoundError:
            pass
        except Exception:
            return None
        _atomic_write(req_path, body)
    except Exception:
        return None
    deadline = time.monotonic() + max(0.1, timeout)
    want = body.get("id") if isinstance(body, dict) else None
    while time.monotonic() < deadline:
        try:
            with open(resp_path, encoding="utf-8") as fh:
                resp = json.load(fh)
            if isinstance(resp, dict) and resp.get("id") == want:
                return resp
        except (FileNotFoundError, ValueError):
            pass
        except Exception:
            return None
        time.sleep(POLL_STEP)
    return None


def cleanup(directory, *names):
    """Best-effort unlink of consumed protocol files. Never raises."""
    for name in names:
        try:
            os.unlink(os.path.join(directory, name))
        except Exception:
            pass
