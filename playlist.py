"""Playlist mode for displayd: automatic rotation with a progress bar.

A playlist is a configured list of views ``[{renderer, params, dwell}]``.
When enabled, a scheduler advances through the list on top of the existing
``/show`` machinery (one ``_start_view`` per switch -- no parallel path),
wrapping at the end. Each view's dwell time is its own.

The progress bar fills monotonically from empty to full across the current
view's dwell; when it reaches full the view switches. It is composited by
the daemon onto every presented frame (see ``Screen.present``), so it
tracks animated views frame-for-frame and is repainted on a short tick for
static views that park after one frame.

Design decisions (captain's brief, task-qll0e):

- **Fill, not deplete (default).** The captain described both a shrinking
  countdown and a loading bar that "maximums and then changes". The default
  is a growing fill bar: at switch time the bar is full, which reads
  unambiguously as "this view's time is up". A shrinking bar starts full
  and could be mistaken for a border. ``direction: "drain"`` reverses it
  for operators who prefer the countdown reading; the two are never mixed.
- **Fill direction follows the edge.** ``top``/``bottom`` fill left to
  right; ``left``/``right`` fill bottom to top (like a meter rising). Drain
  mode empties in the mirror direction.
- **Flush to the edge, thin by default.** 10px at 1080p, configurable
  2..64. No inset: the bar owns its edge strip outright instead of floating
  over content with margins.
- **Smoothness.** The bar is repainted on ``tick_seconds`` (default 0.2s,
  i.e. 5Hz). A full-width bar over a 30s dwell advances ~13px per tick --
  below what the eye resolves at wall distance -- while a full-frame
  present at 5Hz is ~40MB/s of framebuffer writes, comfortably cheap.
- **Colour conformance (Option A, additive).** A renderer may declare an
  ``ACCENT`` module attribute (``"#rrggbb"``, a colour name, or an
  ``(r, g, b)`` tuple) naming the colour the bar should wear on that view.
  Resolution order: per-view ``color`` in the playlist item (operator
  override, e.g. for views owned by other tasks) > renderer ``ACCENT`` >
  playlist-level ``color`` default > white fallback. A renderer that
  declares nothing keeps working exactly as before. Whatever colour wins,
  the fill is drawn with a contrast border (black on bright accents, white
  on dark ones) over a dark track, so the bar reads on both dark views
  (beads) and light ones (qr) instead of vanishing into the background.
- **Rotation yields.** While a notice/attention transient holds the screen,
  while the panel is blanked (manual off or idle-off), or after an explicit
  manual ``/show``/``/clear``, the scheduler holds: it does not advance,
  and the bar is hidden. Transients and blanking resume automatically with
  a fresh dwell; a manual choice holds until an explicit resume (the
  manual-choice-wins rule). Advancing never touches the activity clock, so
  rotation cannot defeat idle-off.
- **Broken views don't stall.** An unknown renderer is skipped after a
  short error dwell; a renderer that raises mid-dwell holds its last good
  frame (existing ``_run`` containment) and the scheduler still advances on
  time.

Renderer contract (additive -- declare nothing and nothing changes)::

    ACCENT = "#4DC3FF"   # or "green", or (77, 195, 255)

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

PLACEMENTS = ("top", "left", "bottom", "right")
DIRECTIONS = ("fill", "drain")

DEFAULT_COLOR = (255, 255, 255)
TRACK_COLOR = (38, 38, 46)
ERROR_DWELL = 5.0  # seconds to linger (bar hidden) on an unshowable view
HISTORY = 50

NAMED = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "grey": (128, 128, 128),
    "gray": (128, 128, 128),
    "orange": (255, 165, 0),
}


def parse_color(value, default=None):
    """Accept '#rgb', '#rrggbb', a few names, or an (r,g,b) tuple."""
    if value is None or value == "":
        return default
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return tuple(max(0, min(255, int(v))) for v in value)
        except (TypeError, ValueError):
            return default
    text = str(value).strip()
    if text.lower() in NAMED:
        return NAMED[text.lower()]
    digits = text.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    if len(digits) == 6:
        try:
            return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return default
    return default


def accent_for(renderer_entry, item_color=None, default=DEFAULT_COLOR):
    """Resolve the bar colour for one view.

    ``renderer_entry`` is a registry entry as built by
    ``displayd.load_renderers`` (``{"module": mod, ...}``); entries without
    a module (broken plugins) fall through to the default. Precedence:
    per-view item ``color`` > renderer ``ACCENT`` > playlist default.
    Never raises: garbage resolves to the default."""
    for candidate in (item_color,
                      getattr((renderer_entry or {}).get("module"), "ACCENT", None)
                      if isinstance(renderer_entry, dict) else None,
                      default):
        parsed = parse_color(candidate, None)
        if parsed is not None:
            return parsed
    return DEFAULT_COLOR


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


class Playlist:
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

    # ---- state ---------------------------------------------------------

    def _config(self):
        try:
            return self.daemon.policy.get_config()["playlist"]
        except Exception:
            return {}

    def _views(self, cfg):
        views = cfg.get("views") or []
        return views if isinstance(views, list) else []

    def _hold_reason(self, cfg):
        """Why the scheduler must not advance right now (None = run)."""
        if not cfg.get("enabled"):
            return "disabled"
        if not self._views(cfg):
            return "empty"
        with self._lock:
            if self.paused_by is not None:
                return "paused:manual"
        try:
            if self.daemon.policy.transient_status().get("active") is not None:
                return "transient"
        except Exception:
            pass
        try:
            if getattr(self.daemon.fb, "blanked", False):
                return "screen-off"
        except Exception:
            pass
        return None

    def progress(self):
        """Filled share of the current dwell in [0, 1], or None when the
        bar should be hidden (held, error dwell, or misconfigured)."""
        cfg = self._config()
        if self._hold_reason(cfg) is not None:
            return None
        with self._lock:
            if self.item_started is None or not self.current_ok:
                return None
            if self.dwell <= 0:
                return None
            return max(0.0, min(1.0, (self.clock() - self.item_started)
                                / self.dwell))

    def status(self):
        cfg = self._config()
        views = self._views(cfg)
        with self._lock:
            idx = self.index if views else 0
            item = views[idx % len(views)] if views else None
            frac = self.progress()
            history = list(self.history[-10:])
            paused = self.paused_by
            err = self.last_error
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "hold": self._hold_reason(cfg),
            "paused_by": paused,
            "placement": cfg.get("placement"),
            "thickness": cfg.get("thickness"),
            "direction": cfg.get("direction"),
            "index": idx,
            "view": item,
            "progress": round(frac, 4) if frac is not None else None,
            "last_error": err,
            "history": [{"at": at, "renderer": name} for at, name in history],
        }

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

    # ---- scheduler ---------------------------------------------------------

    def _show_current(self, item, dwell):
        name = item.get("renderer")
        params = item.get("params") or {}
        try:
            self.daemon.policy.note_playlist(name, params)
            self.daemon._start_view(name, params)
        except (KeyError, ValueError) as err:
            with self._lock:
                self.last_error = "%s: %s" % (type(err).__name__, err)
                self.item_started = self.clock()
                self.dwell = ERROR_DWELL
                self.current_ok = False
            return
        except Exception as err:  # never let a view kill the scheduler
            with self._lock:
                self.last_error = "%s: %s" % (type(err).__name__, err)
                self.item_started = self.clock()
                self.dwell = ERROR_DWELL
                self.current_ok = False
            return
        with self._lock:
            self.last_error = None
            self.item_started = self.clock()
            self.dwell = dwell
            self.current_ok = True
            self.history.append((time.time(), name))
            del self.history[:-HISTORY]

    def _loop(self):
        while not self._stop.is_set():
            cfg = self._config()
            tick = cfg.get("tick_seconds") or 0.2
            try:
                tick = float(tick)
            except (TypeError, ValueError):
                tick = 0.2
            tick = max(0.05, min(2.0, tick))
            if self._hold_reason(cfg) is not None:
                with self._lock:
                    self.item_started = None
                    self.current_ok = False
                self._stop.wait(tick)
                continue
            views = self._views(cfg)
            sig = repr([(v.get("renderer"), v.get("dwell"),
                         repr(v.get("params"))) for v in views])
            with self._lock:
                if sig != self._views_sig:
                    self._views_sig = sig
                    self.index = 0
                    self.item_started = None
                if self.index >= len(views):
                    self.index = 0
                started = self.item_started
                # Effective dwell: a failed show shortens to ERROR_DWELL
                # inside _show_current, so read it back under the lock.
                eff_dwell = self.dwell
                idx = self.index
            item = views[idx % len(views)]
            try:
                dwell = float(item.get("dwell", 30))
            except (TypeError, ValueError):
                dwell = 30.0
            if started is None:
                self._show_current(item, dwell)
            elif self.clock() - started >= eff_dwell:
                with self._lock:
                    self.index = (self.index + 1) % len(views)
                    self.item_started = None
            else:
                try:
                    self.daemon.screen.repaint_overlay()
                except Exception:
                    pass
            self._stop.wait(tick)
