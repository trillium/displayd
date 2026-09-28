"""Playlist mode for displayd: automatic rotation with a progress bar.

A playlist is a configured list of views ``[{renderer, params, dwell}]``.
The scheduler advances through the list on top of the existing ``/show``
machinery (one ``_start_view`` per switch, wrapping at the end).

Single-concept split: colours live in :mod:`playlist_color`, bar geometry
and compositing in :mod:`playlist_bar`, the read model (hold reasons,
progress, status) in :mod:`playlist_state`, and view advancement in
:mod:`playlist_schedule`. This module keeps the ``Playlist`` identity,
lifecycle, overlay hook, and view-list validation; moved names are
re-exported here so existing importers keep working.

Config lives in the ``playlist`` section of the policy surface
(``GET``/``POST /policy``, persisted to ``policy.json``)::

    {"playlist": {"enabled": True, "placement": "bottom", "thickness": 10,
                  "direction": "fill", "color": "#FFFFFF",
                  "tick_seconds": 0.2,
                  "views": [{"renderer": "beads", "params": {}, "dwell": 30},
                            {"renderer": "clock", "dwell": 15}]}}
"""

import threading
import time

from playlist_bar import (DIRECTIONS, PLACEMENTS, bar_boxes, draw_bar)
from playlist_color import (DEFAULT_COLOR, NAMED, TRACK_COLOR, accent_for,
                            parse_color)
from playlist_schedule import (ERROR_DWELL, HISTORY, PlaylistScheduleMixin)
from playlist_state import PlaylistStateMixin


def validate_views(views):
    """Validate a playlist view list. Raises ValueError on mismatch."""
    if not isinstance(views, list):
        raise ValueError("playlist.views must be a list")
    for i, item in enumerate(views):
        where = "playlist.views[%d]" % i
        if not isinstance(item, dict):
            raise ValueError("%s must be an object" % where)
        name = item.get("renderer")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("%s.renderer must be a non-empty string" % where)
        if "params" in item and not isinstance(item["params"], dict):
            raise ValueError("%s.params must be an object" % where)
        dwell = item.get("dwell", 30)
        if isinstance(dwell, bool) or not isinstance(dwell, (int, float)):
            raise ValueError("%s.dwell must be a number of seconds" % where)
        if not (3 <= dwell <= 3600):
            raise ValueError("%s.dwell must be within [3, 3600]" % where)
        if "color" in item and parse_color(item["color"], None) is None:
            raise ValueError("%s.color is not a colour" % where)
    return views


def bar_boxes(width, height, placement, thickness, fraction):
    """Return (track_box, fill_box) in PIL coordinates.

    The track owns a flush strip along ``placement``; the fill grows
    left-to-right on horizontal edges and bottom-to-top on vertical ones.
    ``fraction`` is the filled share in [0, 1] (already direction-applied
    by the caller). Either box may be ``None`` when there is nothing to
    draw (zero thickness or empty fill)."""
    t = max(1, int(thickness))
    fraction = max(0.0, min(1.0, fraction))
    if placement == "top":
        track = (0, 0, width, t)
        w = int(width * fraction)
        fill = (0, 0, w, t) if w > 0 else None
    elif placement == "bottom":
        track = (0, height - t, width, height)
        w = int(width * fraction)
        fill = (0, height - t, w, height) if w > 0 else None
    elif placement == "left":
        track = (0, 0, t, height)
        h = int(height * fraction)
        fill = (0, height - h, t, height) if h > 0 else None
    elif placement == "right":
        track = (width - t, 0, width, height)
        h = int(height * fraction)
        fill = (width - t, height - h, width, height) if h > 0 else None
    else:
        raise ValueError("placement must be one of %s" % "/".join(PLACEMENTS))
    return track, fill


def _contrast(color):
    """Border colour that reads against both the accent and any page."""
    lum = (0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]) / 255.0
    return (10, 10, 12) if lum > 0.55 else (235, 235, 240)


def draw_bar(img, placement, thickness, fraction, direction, color):
    """Composite the progress bar onto a PIL image, in place. Returns img."""
    from PIL import ImageDraw

    shown = fraction if direction != "drain" else 1.0 - fraction
    track, fill = bar_boxes(img.size[0], img.size[1], placement,
                            thickness, shown)
    draw = ImageDraw.Draw(img)
    if track is not None:
        draw.rectangle(track, fill=TRACK_COLOR)
    if fill is not None:
        draw.rectangle(fill, fill=tuple(color))
        draw.rectangle(fill, outline=_contrast(tuple(color)), width=1)
    return img


class Playlist(PlaylistStateMixin, PlaylistScheduleMixin):
    """Scheduler advancing through configured views on top of /show.

    Constructed with the daemon (duck-typed: ``.policy``, ``.screen``,
    ``.fb``, ``.renderers``, ``._start_view``). Runs its own daemon thread;
    all state changes are lock-guarded. Uses ``time.monotonic`` (injectable)
    for dwell deadlines so wall-clock steps never stretch a dwell.
    """

    def __init__(self, daemon, clock=None):
        self.daemon = daemon
        self.clock = clock or time.monotonic
        # RLock: status()/progress() compose _hold_reason() with more
        # locked reads; a plain Lock would deadlock on that nesting.
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self.index = 0
        self.item_started = None
        self.dwell = 0
        self.current_ok = False
        self.paused_by = None  # "manual" after an explicit /show or /clear
        self.last_error = None
        self.history = []  # [(wall_time, renderer)] newest last, bounded
        self._views_sig = None

    # ---- lifecycle ---------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="playlist")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread = None

    def boot(self):
        """Fresh daemon start is not a manual choice: clear any pause so a
        persisted ``enabled: true`` resumes rotating after a restart."""
        with self._lock:
            self.paused_by = None

    def on_manual(self):
        """An explicit /show or /clear wins: hold the rotation."""
        with self._lock:
            self.paused_by = "manual"

    def config_updated(self, patch):
        """An operator saving the playlist section with it enabled means
        \"run\": clear a manual hold. Never enables the playlist itself."""
        if not isinstance(patch, dict):
            return
        section = patch.get("playlist")
        if not isinstance(section, dict):
            return
        if section.get("enabled"):
            with self._lock:
                self.paused_by = None

    def pause(self):
        with self._lock:
            self.paused_by = "manual"

    def resume(self):
        with self._lock:
            self.paused_by = None
            self.item_started = None  # fresh dwell on the current view

    def next(self):
        """Skip to the next view now (operator nudge, not a manual hold)."""
        with self._lock:
            self.item_started = None
            self.index += 1

    # ---- overlay ---------------------------------------------------------

    def overlay_image(self, img):
        """``Screen.overlay`` hook: composite the bar when dwelling.

        Runs on the present path, so it never blocks, never raises, and
        never allocates beyond one FB-sized draw. Returns img untouched
        when the bar should be hidden."""
        try:
            frac = self.progress()
            if frac is None:
                return img
            cfg = self._config()
            placement = cfg.get("placement") or "bottom"
            if placement not in PLACEMENTS:
                placement = "bottom"
            thickness = cfg.get("thickness") or 10
            direction = cfg.get("direction") or "fill"
            with self._lock:
                views = self._views(cfg)
                item = views[self.index % len(views)] if views else None
            entry = self.daemon.renderers.get((item or {}).get("renderer"))
            color = accent_for(entry, (item or {}).get("color"),
                               cfg.get("color") or DEFAULT_COLOR)
            return draw_bar(img, placement, thickness, frac, direction,
                            color)
        except Exception:
            return img
