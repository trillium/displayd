"""Playlist mode for displayd: automatic rotation with a progress bar.

A playlist is a configured list of views ``[{renderer, params, dwell}]``.
The scheduler advances through the list on top of the existing ``/show``
machinery (one ``_start_view`` per switch, wrapping at the end).

Single-concept split: colours live in :mod:`playlist_color`, the read
model (hold reasons, progress, status) in :mod:`playlist_state`, view
advancement in :mod:`playlist_schedule`, and the bar itself -- geometry
and compositing -- in the component layer,
:mod:`renderers.ui.progress`. The bar's own names are re-exported here
so existing importers keep working, and nothing in this module draws.

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

from playlist_color import (DEFAULT_COLOR, NAMED, accent_for, parse_color)
from playlist_schedule import (ERROR_DWELL, HISTORY, PlaylistScheduleMixin)
from playlist_state import PlaylistStateMixin
from renderers.ui import progress as ui_progress

# The bar is a component: this module composes it, it does not draw it.
# Re-exported because the playlist's own config validation and its
# callers name the vocabulary and the geometry through this module.
PLACEMENTS = ui_progress.PLACEMENTS
DIRECTIONS = ui_progress.DIRECTIONS
bar_boxes = ui_progress.boxes
draw_bar = ui_progress.draw


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
