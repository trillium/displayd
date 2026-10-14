"""Fonts for the html painter: face resolution and the bounded, explicitly
released font cache litehtml's create_font/delete_font calls drive.
"""

import os
import threading

from PIL import ImageFont

# Panel-safe defaults. A font family in a template that does not resolve to
# one of these files falls back down the list, and finally to PIL's bundled
# face, so text is never measured with a stub.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)
MAX_FONTS = 32


def _font_file(weight, style):
    """Resolve a CSS weight/style pair to a real TTF, or None."""
    bold = weight >= 600
    for path in FONT_CANDIDATES:
        name = os.path.basename(path).lower()
        is_bold = "bold" in name
        is_italic = "italic" in name or "oblique" in name
        if is_bold == bold and is_italic == bool(style & 1):
            if os.path.exists(path):
                return path
    return None


def ui_font(size, bold=True):
    """A panel-safe PIL font, for a view's own chrome.

    Same candidate list the document text uses, so an html view and the
    rest of the panel never disagree about what a face is. Never returns
    None: PIL's bundled face is the last resort, because a missing font
    must not turn into a missing label.
    """
    path = _font_file(700 if bold else 400, 0)
    try:
        return ImageFont.truetype(path, size) if path else \
            ImageFont.load_default(size=size)
    except OSError:
        return ImageFont.load_default()


def _family_key(family):
    """First CSS family name, lowercased; generic families keep their name."""
    raw = (family or b"").decode("utf-8", "replace")
    first = raw.split(",")[0].strip().strip("'\"").lower()
    return first or "sans-serif"

class _FontEntry:
    __slots__ = ("image_font", "ascent", "descent", "ch_width", "widths",
                 "handle")

    def __init__(self, image_font, ascent, descent, ch_width):
        self.image_font = image_font
        self.ascent = ascent
        self.descent = descent
        self.ch_width = ch_width
        self.widths = {}
        self.handle = None

    def width(self, text):
        """Advance width of a run, memoised per string.

        litehtml asks for the same short strings many times per layout
        (once per wrap candidate); PIL's getlength is a real shaping call,
        so the memo is worth it and bounded by the layout, not by time.
        """
        got = self.widths.get(text)
        if got is None:
            got = self.image_font.getlength(text)
            self.widths[text] = got
        return got


class _FontCache:
    """Bounded, explicitly released font cache.

    The point of this class is the release path: litehtml calls
    delete_font for every font a document created, so entries leave the
    cache the moment the document dies and steady-state memory is flat by
    construction rather than by hoping. MAX_FONTS is the backstop for a
    document that asks for an unbounded number of sizes.
    """

    live = 0
    """Fonts handed out across every cache alive right now.

    Class-level so a leak is observable from outside a render: after a
    render returns this must be back to 0, because litehtml's document
    destructor calls delete_font for every font it created. The flat-RSS
    claim rests on that, so it is machine-checked rather than assumed.
    """
    _live_lock = threading.Lock()

    def __init__(self, limit=MAX_FONTS):
        self.limit = limit
        self._lock = threading.Lock()
        self._next = 1
        self._entries = {}

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def create(self, descr):
        size = max(1, min(1024, int(round(descr.size))))
        family = _family_key(descr.family)
        key = (family, size, descr.style & 1, descr.weight)
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None:
                return hit
        entry = _make_font(family, size, descr)
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None:
                return hit
            self._next += 1
            entry.handle = self._next
            self._entries[key] = entry
            while len(self._entries) > self.limit:
                self._entries.pop(next(iter(self._entries)))
            with _FontCache._live_lock:
                _FontCache.live += 1
        return entry

    def release(self, handle):
        with self._lock:
            for key, entry in list(self._entries.items()):
                if entry.handle == handle:
                    del self._entries[key]
                    with _FontCache._live_lock:
                        _FontCache.live = max(0, _FontCache.live - 1)
                    return

    def width(self, handle, text):
        with self._lock:
            entry = self._by_handle(handle)
        if entry is None:
            return 0.0
        return entry.width(text)

    def font(self, handle):
        with self._lock:
            return self._by_handle(handle)

    def _by_handle(self, handle):
        for entry in self._entries.values():
            if entry.handle == handle:
                return entry
        return None


def _make_font(family, size, descr):
    """A real TrueType face when one is installed, PIL's bundled one if not."""
    path = _font_file(descr.weight, descr.style)
    try:
        if path:
            face = ImageFont.truetype(path, size)
        else:
            face = ImageFont.load_default(size=size)
        ascent, descent = face.getmetrics()
        ch_width = float(face.getlength("0"))
    except Exception:
        # A broken font must not take the frame down: fall back to the
        # metrics of the default face at the same size.
        face = ImageFont.load_default()
        ascent, descent = face.getmetrics()
        ch_width = float(face.getlength("0"))
    return _FontEntry(face, ascent, descent, ch_width)
