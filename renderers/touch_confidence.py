"""Touchscreen confidence mode for the lnx-server jumbotron.

Opt-in, visible tap-test view: draws every configured touch region as a
labelled box (id + action), plus title/instructions, so an operator on
the panel can see exactly where to tap. Live tap diagnostics arrive
through POST /feed/touch_confidence/tap: last tap coordinates, matched
region/action or dead-zone result, counters, action result/error.

Regions come from the selection params (mirroring touch.json), NOT from
a feed. Malformed entries are skipped, never fatal. Isolated by design:
this renderer reads its own tap feed only and never touches the policy
clock, the playlist, or any other view's buffer.

Split across touch_confidence_*.py helpers (regions, taps, draw) to
stay within the project's 250-line budget; names used by existing
tests are re-exported here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from touch_confidence_draw import draw
from touch_confidence_regions import (
    REGION_COLORS,
    coerce_regions,
    format_label,
)
from touch_confidence_taps import _last_line, summarize, valid_taps

NAME = "touch_confidence"
DESCRIPTION = (
    "Touchscreen confidence: region map + live tap diagnostics "
    "(feed POST /feed/touch_confidence/tap)"
)
STATIC = False
ACCENT = "#50DC78"
PARAMS = {
    "title": {"type": "string", "help": "header text, default TOUCH CONFIDENCE"},
    "instructions": {
        "type": "string",
        "help": "sub-header, default 'tap a zone, watch it register'",
    },
    "regions": {
        "type": "array",
        "help": "touch regions to draw: [{id, rect: [x, y, w, h], "
        "action}] in display pixels; malformed entries skipped",
    },
    "width": {
        "type": "integer",
        "help": "coordinate width the region rects are written in; "
        "default is the screen width (no scaling)",
    },
    "height": {
        "type": "integer",
        "help": "coordinate height the region rects are written in; "
        "default is the screen height (no scaling)",
    },
    "background": {"type": "string", "help": "background colour, default near-black"},
}
INPUTS = {
    "tap": {
        "type": "object",
        "help": "one resolved tap: {x, y, region?, action?, hit?, ...}",
        "required": ["x", "y"],
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "x_norm": {"type": "number"},
            "y_norm": {"type": "number"},
            "region": {"type": "string"},
            "action": {"type": "string"},
            "hit": {"type": "boolean"},
            "result": {"type": "string"},
            "error": {"type": "string"},
            "ts": {"type": "number"},
        },
        "buffer": 200,
    },
}

POLL = 0.2  # seconds between buffer checks; draws happen only on change

__all__ = [
    "NAME",
    "DESCRIPTION",
    "STATIC",
    "ACCENT",
    "PARAMS",
    "INPUTS",
    "POLL",
    "DEFAULT_TITLE",
    "DEFAULT_INSTRUCTIONS",
    "REGION_COLORS",
    "coerce_regions",
    "format_label",
    "valid_taps",
    "summarize",
    "_last_line",
    "draw",
    "run",
]

DEFAULT_TITLE = "TOUCH CONFIDENCE"
DEFAULT_INSTRUCTIONS = "tap a zone \u2014 watch it register here"


def run(screen, params, stop):
    """Confidence loop: redraw only when taps or regions change."""
    params = params or {}
    title = str(params.get("title") or DEFAULT_TITLE)
    instructions = str(params.get("instructions") or DEFAULT_INSTRUCTIONS)
    bg = screen.color(params.get("background"), (10, 10, 14))
    accent = screen.color(ACCENT, (80, 220, 120))
    regions = coerce_regions(params, screen.W, screen.H)

    last_key = None
    while not stop.is_set():
        taps = valid_taps(screen)
        summary = summarize(taps)
        last = summary["last"]
        try:
            last_repr = repr(sorted(last.items())) if last else None
        except Exception:
            last_repr = repr(last)
        key = (summary["total"], last_repr)
        if key != last_key:
            last_key = key
            # One complete frame, one swap: never a partial confidence card.
            screen.present(draw(screen, title, instructions, regions, summary, bg, accent))
        stop.wait(POLL)
