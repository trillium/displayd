"""Running-app helpers for the merged macbook feature (NOT a renderer).

The standalone ``talon_apps`` view is retired: the app list lives in the
merged macbook header (GLANCE mode), because this was never meant to be
two features. This module keeps the pure helpers the merged feature and
the unified dock share (``clean``/``label``/``groups``) plus the feed
schema (``STATE_SCHEMA``).

Feed compat: the Mac-side poller (``bridges/talon_apps.py``) still posts
``POST /feed/talon_apps/state`` -- that namespace is owned by the merged
macbook feature now (see the daemon's feed compat), so no Mac-side change
was needed. App names come from the OS: treated as untrusted (``clean()``
bounds length and strips control characters) and never interpolated into
a shell anywhere on this path.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import talon_layout

# No run(): the daemon's renderer loader skips this file (same convention
# as unified_dock.py), so GET /renderers advertises one feature only.

STATE_SCHEMA = {
    "type": "object",
    "help": "running-app document (see bridges/talon_apps.py)",
    "required": ["ts", "apps"],
    "properties": {
        "ts": {"type": "number"},
        "apps": {"type": "array",
                 "items": {"type": "string"}},
        "focused": {"type": "string"},
        "windows": {"type": "object"},
        "displays": {"type": "array"},
    },
    "buffer": 1,
}

# Event-driven feed: the bridge heartbeats every 10s, so quiet >30s
# means the bridge is genuinely dead (not just no app launched).
STALE_AFTER = 30.0
HEARTBEAT_AFTER = 10.0
NAME_CHARS = 48
LABEL_CHARS = 26

C_LINE = (60, 60, 70)


def clean(name):
    """Bound one OS app name for display. Never raises."""
    text = name if isinstance(name, str) else ""
    text = "".join(ch for ch in text.strip() if ch.isprintable())
    return text[:NAME_CHARS]


def label(name):
    """One button label: cleaned, truncated with an ellipsis."""
    text = clean(name)
    if len(text) > LABEL_CHARS:
        text = text[:LABEL_CHARS - 1] + "…"
    return text or "unknown"


def groups(state):
    """Side grouping for a feed state (pure; shared with the dock)."""
    state = state if isinstance(state, dict) else {}
    apps = state.get("apps")
    apps = apps if isinstance(apps, list) else []
    return talon_layout.group(apps, state.get("windows"),
                              state.get("displays"))
