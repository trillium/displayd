"""Source resolution for the row renderer: where the streak comes from.

Single concept: every "where" question -- the local rows.txt fallback path,
the primary remote source (mini1 PM5 WebSocket, rows.txt URL, or file), the
fetch timeout, and the sightings-journal path -- with no hardcoded home
paths anywhere on the chain. Pure resolution only; fetching and drawing
live in row.py.
"""

import os
import tempfile
import urllib.parse

ENV_VAR = "DISPLAYD_ROW_FILE"
ENV_SOURCE = "DISPLAYD_ROW_SOURCE"
ENV_JOURNAL = "DISPLAYD_ROW_JOURNAL"
# Primary row source: the mini1 PM5 bridge WebSocket that feeds OBS (same
# feed, no second serving path). Reachable from lnx-server over the tailnet.
DEFAULT_SOURCE = "ws://mini1:8765/obs/ws"
FETCH_TIMEOUT_DEFAULT = 10
FETCH_TIMEOUT_MIN = 2
FETCH_TIMEOUT_MAX = 60

# ---- path resolution (no hardcoded home paths) ---------------------------

def resolve_path(explicit=None):
    """Where to read the row log from. Precedence: explicit `path`
    param, `$DISPLAYD_ROW_FILE`, then a sibling `row_tracker/rows.txt`
    next to (up to three levels above) this displayd checkout -- so the
    pairing survives checkout moves on any host. Returns None when no
    candidate exists; the panel says so instead of going blank."""
    if explicit:
        return str(explicit)
    env = os.environ.get(ENV_VAR)
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))  # .../displayd/renderers
    root = os.path.dirname(here)  # .../displayd
    node = root
    for _ in range(4):
        cand = os.path.join(os.path.dirname(node), "row_tracker", "rows.txt")
        if os.path.exists(cand):
            return cand
        direct = os.path.join(node, "row_tracker", "rows.txt")
        if os.path.exists(direct):
            return direct
        node = os.path.dirname(node)
    return None


# ---- remote source resolution -------------------------------------------

def classify_source(src):
    """Split a `source` value into (kind, target). Kind is one of:
    `ws` (live PM5 stats feed: ws(s) URL, or http(s) URL whose path ends
    in /ws -- the same OBS socket either way), `text-url` (http(s) URL
    serving rows.txt text), or `file` (local path)."""
    s = str(src or "").strip()
    if s.startswith(("ws://", "wss://")):
        return ("ws", s)
    if s.startswith(("http://", "https://")):
        try:
            path = urllib.parse.urlsplit(s).path or ""
        except Exception:
            path = ""
        if path.rstrip("/").endswith("/ws"):
            return ("ws", s)
        return ("text-url", s)
    return ("file", s)


def resolve_source(explicit=None):
    """Which row source to poll. Precedence: explicit `source` param,
    `$DISPLAYD_ROW_SOURCE`, else the mini1 PM5 feed. Never empty."""
    if explicit:
        return str(explicit)
    env = os.environ.get(ENV_SOURCE)
    if env:
        return env
    return DEFAULT_SOURCE


def parse_timeout(value):
    """Remote fetch seconds, clamped to [2, 60]; garbage means 10."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return FETCH_TIMEOUT_DEFAULT
    return max(FETCH_TIMEOUT_MIN, min(FETCH_TIMEOUT_MAX, iv))


def resolve_journal(explicit=None, local_path=None):
    """Where remote sightings are journalled. Precedence: explicit
    `journal` param, `$DISPLAYD_ROW_JOURNAL`, a `row_sightings.json`
    next to the local log, else the system temp dir. No hardcoded home
    paths anywhere on this chain."""
    if explicit:
        return str(explicit)
    env = os.environ.get(ENV_JOURNAL)
    if env:
        return env
    if local_path:
        try:
            return os.path.join(
                os.path.dirname(os.path.abspath(local_path)),
                "row_sightings.json")
        except Exception:
            pass
    return os.path.join(tempfile.gettempdir(),
                        "displayd-row-sightings.json")


def source_label(kind, target, path):
    """One-line footer naming the active source chain for the panel."""
    if kind == "ws":
        base = "live %s" % target
    elif kind == "text-url":
        base = target
    else:
        base = target or ""
    if path and (kind != "file" or path != target):
        if base:
            return "%s + %s" % (base, path)
        return path
    return base or "no log configured"
