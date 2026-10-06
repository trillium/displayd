"""Presentation: the Screen handle renderers draw through, and RegionScreen.

Single concept: getting one composed frame onto the panel -- compose-then-swap
so a single framebuffer write is never blank or partial, the shared chrome
overlay chain, the frame-cache hook, and per-region presentation for
static-region layouts. Screen is also the object renderers receive.
"""

import os
import threading

from PIL import Image

class Screen:
    """The handle handed to renderers, plus the shared helpers they need."""

    NAMED_COLORS = {
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

    def __init__(self, fb):
        self.fb = fb
        self.W = fb.width
        self.H = fb.height
        self.present_lock = threading.Lock()
        self.feeds = None  # wired by DisplayDaemon to its FeedStore
        self.current_view = None
        self.on_present = None  # daemon hook(img): frame cache + switch timing
        self.overlay = None  # playlist progress bar hook: fn(img) -> img
        self._base = None  # last pre-overlay frame, for overlay repaints

    def new_image(self, background=(0, 0, 0)):
        return Image.new("RGB", (self.W, self.H), background)

    def present(self, img):
        """Compose-then-swap: the caller hands over one complete frame and
        the daemon swaps it in with a single write -- never a blank or a
        partial frame."""
        with self.present_lock:
            if self.overlay is None:
                self._base = None
                self.fb.present(img)
            else:
                try:
                    self._base = img.copy()
                    self.fb.present(self.overlay(img.copy()))
                except Exception:
                    self._base = None
                    self.fb.present(img)
            hook = self.on_present
        if hook is not None:
            try:
                # Pre-overlay frame: the frame cache stays bar-free, so a
                # cached re-entry never serves a stale progress bar.
                hook(img)
            except Exception:
                pass

    def repaint_overlay(self):
        """Re-composite the overlay onto the last frame and present.

        Lets the playlist bar advance smoothly on STATIC views that park
        after a single present. Best-effort: never raises. Bypasses the
        on_present hook on purpose: overlay ticks are not fresh draws and
        must not pollute the frame cache or switch timing."""
        with self.present_lock:
            if self.overlay is None or self._base is None:
                return
            try:
                self.fb.present(self.overlay(self._base.copy()))
            except Exception:
                pass

    def clear(self, background=(0, 0, 0)):
        self.present(self.new_image(background))

    def get_input(self, renderer, input_name):
        """Buffered payloads pushed to (renderer, input), oldest first.
        Empty when nothing has arrived yet -- renderers must handle that."""
        store = self.feeds
        if store is None:
            return []
        try:
            return store.get(renderer, input_name)
        except Exception:
            return []

    @classmethod
    def color(cls, value, default=(255, 255, 255)):
        """Accept '#rgb', '#rrggbb', a few names, or an (r,g,b) tuple."""
        if value is None or value == "":
            return default
        if isinstance(value, (list, tuple)) and len(value) == 3:
            return tuple(int(v) for v in value)
        text = str(value).strip()
        if text.lower() in cls.NAMED_COLORS:
            return cls.NAMED_COLORS[text.lower()]
        digits = text.lstrip("#")
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        try:
            return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return default

    @staticmethod
    def font_path(family="DejaVuSans-Bold"):
        candidates = [
            os.path.join("/usr/share/fonts/truetype/dejavu", family + ".ttf"),
            os.path.join("/usr/share/fonts/truetype/dejavu", "DejaVuSans-Bold.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None


class RegionScreen(Screen):
    """A Screen bound to one layout region (ISA D6 option B).

    Same renderer contract -- W/H, new_image, present, clear, get_input,
    color, font_path -- but scoped to the region's pixel box. present()
    caches this region's frame and recomposites the whole panel from the
    per-region cache, so updating one region never disturbs the others."""

    def __init__(self, daemon, region_name, width, height):
        self.fb = daemon.fb
        self.W = width
        self.H = height
        self.present_lock = daemon.screen.present_lock
        self.feeds = daemon.feeds
        self.current_view = None  # set to the bound renderer on start
        self.on_present = None  # unused: regions report via _present_region
        self._daemon = daemon
        self.region_name = region_name

    def present(self, img):
        self._daemon._present_region(self.region_name, img)
