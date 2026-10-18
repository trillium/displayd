"""What one switch-latency measurement needs, per view (the fixture table).

Single concept: the inputs a view is given so its FIRST paint is the paint a
panel actually shows -- the params it is selected with and the feed payloads
buffered before the switch. Nothing here is a rendering decision; it is the
material one measurement run needs, kept out of the harness so the harness
stays about timing.

The table is exhaustive on purpose: it must name EVERY view the renderer
registry offers. A view with no fixture is not "skipped", it is a hole in the
gate, and the harness fails on it. That is what keeps the litehtml migration
from quietly adding a view nobody ever measured.

``needs_engine`` marks the views whose honest paint requires the litehtml
shared library. Without the library they draw a red build-hint card, which is
fast and says nothing about the view -- so the harness names them as not
measured here rather than banking the error card's latency as if it were the
view's.

``params`` may be a callable taking the run's scratch directory, for the two
views that can only be shown against a real file (``image`` needs a picture,
``row`` needs a rows.txt so it never reaches for the tailnet).
"""

import base64
import io
import os
import time


def _png(scratch, width=96, height=96, colour=(40, 90, 160)):
    """A tiny real PNG written under `scratch`, and the same bytes."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, format="PNG")
    data = buf.getvalue()
    path = os.path.join(scratch, "fixture.png")
    with open(path, "wb") as handle:
        handle.write(data)
    return path, data


def macbook_state():
    return {
        "ts": time.time(),
        "accessibility_trusted": True,
        "focus": {"app_name": "WezTerm", "window_title": "macbookpro: fm-",
                  "window_bounds": {"x": 0, "y": -1049, "w": 1920,
                                    "h": 1049}, "display_index": 1},
        "mouse": {"x": 464, "y": -283, "display_index": 1},
        "displays": [{"bounds": {"x": 0, "y": 0, "w": 1728, "h": 1117},
                      "main": True},
                     {"bounds": {"x": -355, "y": -1080, "w": 1920,
                                 "h": 1080}, "main": False}],
        "talon": {"mode": "command", "muted": False},
    }


def talon_state():
    return {"ts": time.time(), "apps": ["Safari", "Terminal", "Mail"],
            "focused": "Safari"}


def _image_params(scratch):
    return {"path": _png(scratch)[0]}


def _stream_feeds(scratch):
    return {"frame": {"data": base64.b64encode(_png(scratch)[1]).decode()}}


def _row_params(scratch):
    path = os.path.join(scratch, "rows.txt")
    with open(path, "w") as handle:
        handle.write("2026-01-01 1000m 2:00 /5000m\n")
    return {"path": path, "source": path, "journal": os.path.join(
        scratch, "row-journal.json")}


def _html_params(_scratch):
    return {"template": "status.html",
            "vars": {"title": "LATENCY", "sub": "gate", "value": "100",
                     "unit": "ms", "note": "switch -> first pixel"}}


# view -> {"params": {...} | callable(scratch), "feeds": {...} | callable,
#          "needs_engine": bool}
VIEWS = {
    "activity": {"feeds": {"event": {"tool": "bash", "outcome": "ok",
                                     "summary": "latency gate"}}},
    "beads": {},
    "beads-detail": {"params": {"focus": "task-nh3y"}},
    "chat": {"needs_engine": True,
             "feeds": {"message": {"author": "fm", "text": "gate hello"},
                       "roster": {"viewers": [{"id": "1",
                                               "display_name": "fm"}],
                                  "count": 1}}},
    "clock": {},
    "feed_health": {},
    "html": {"needs_engine": True, "params": _html_params},
    "image": {"params": _image_params},
    "life": {},
    "macbook": {"feeds": {"state": macbook_state()},
                "extra_feeds": {"talon_apps": {"state": talon_state()}}},
    "notice": {"params": {"title": "GATE", "body": "measuring",
                          "severity": "info"}},
    "options": {"needs_engine": True},
    "picker": {"needs_engine": True},
    "qr": {"params": {"data": "https://example.invalid/gate",
                      "caption": "gate"}},
    "reload": {"params": {"sha": "0" * 40,
                          "highlights": "latency gate"}},
    "resources": {},
    "retro_grid": {},
    "review": {"needs_engine": True, "params": {"bead": "review-gate"}},
    "row": {"params": _row_params},
    "services": {"params": {"url": "http://127.0.0.1:1/inventory",
                            "interval": 3600}},
    "sleep": {},
    "solid": {"params": {"color": "#101820"}},
    "stream": {"params": {"fps": 2}, "feeds": _stream_feeds},
    "text": {"params": {"text": "LATENCY GATE", "size": 64}},
    "touch_confidence": {"params": {"width": 1920, "height": 1080}},
    "unified": {"needs_engine": True},
}
