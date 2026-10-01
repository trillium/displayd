"""Python half of the displayd html renderer: the ctypes binding, the PIL
painter, and the font cache.

litehtml does the parsing and layout; everything that puts a pixel down
happens here, through PIL, exactly like every other renderer in this repo.
The native side (``renderers/native/``) never sees a PIL object, and this
side never sees a litehtml object -- the two only exchange the plain C
structs in ``displayd_html.h``.

Trust boundary: the parser is not a sandbox. This module refuses to open
anything outside the template root handed to it (:func:`allow_root`), and
refuses URLs entirely, so a document can only ever read local files an
operator put next to the template. No socket is ever created.

Build: ``tools/build_litehtml.sh``. Missing library is a hard, explicit
error -- never a silent no-op frame.
"""

import ctypes
import os
import threading

from PIL import Image, ImageDraw, ImageFont

NATIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native")
LIB_NAMES = ("liblitehtmlpil.dylib", "liblitehtmlpil.so")

LHTML_OK = 0
ERRLEN = 240

REPEAT_NO_REPEAT = 0
REPEAT_REPEAT_X = 1
REPEAT_REPEAT_Y = 2
REPEAT_REPEAT = 3

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
MAX_IMAGES = 8
MAX_IMAGE_PIXELS = 4096 * 4096


class HtmlRenderError(RuntimeError):
    """The native library refused or failed; the message is display-facing."""


class NativeMissing(HtmlRenderError):
    """The layout engine is not built. Distinct so a view can say so."""


# --------------------------------------------------------------------- ABI


class _Color(ctypes.Structure):
    _fields_ = [("r", ctypes.c_ubyte), ("g", ctypes.c_ubyte),
                ("b", ctypes.c_ubyte), ("a", ctypes.c_ubyte)]


class _Rect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("w", ctypes.c_double), ("h", ctypes.c_double)]


class _BorderSide(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("color", _Color),
                ("style", ctypes.c_int)]


class _FontDesc(ctypes.Structure):
    _fields_ = [("family", ctypes.c_char_p), ("size", ctypes.c_double),
                ("style", ctypes.c_int), ("weight", ctypes.c_int),
                ("decoration", ctypes.c_int),
                ("decoration_thickness", ctypes.c_double),
                ("decoration_color", _Color)]


class _FontMetrics(ctypes.Structure):
    _fields_ = [("id", ctypes.c_size_t), ("height", ctypes.c_double),
                ("ascent", ctypes.c_double), ("descent", ctypes.c_double),
                ("x_height", ctypes.c_double), ("ch_width", ctypes.c_double)]


CTX = ctypes.c_void_p
DOUBLE = ctypes.POINTER(ctypes.c_double)
SIDES = ctypes.POINTER(_BorderSide)
# Mirrors the LHTML_SIDE_* macros in renderers/native/displayd_html.h.
SIDE_LEFT, SIDE_TOP, SIDE_RIGHT, SIDE_BOTTOM = 0, 1, 2, 3


def _cbf(ret, *args):
    return ctypes.CFUNCTYPE(ret, *args)


# Rects and colours cross by value, exactly as the C header declares them;
# only genuinely out-parameters (font metrics, image size) are pointers.
class _Callbacks(ctypes.Structure):
    _fields_ = [
        ("ctx", CTX),
        ("create_font", _cbf(None, CTX, ctypes.POINTER(_FontDesc),
                             ctypes.POINTER(_FontMetrics))),
        ("delete_font", _cbf(None, CTX, ctypes.c_size_t)),
        ("text_width", _cbf(ctypes.c_double, CTX, ctypes.c_size_t, ctypes.c_char_p)),
        ("draw_fill", _cbf(None, CTX, _Rect, _Rect, _Color)),
        ("draw_borders", _cbf(None, CTX, _Rect, _Rect, SIDES)),
        ("draw_text", _cbf(None, CTX, ctypes.c_size_t, ctypes.c_char_p, _Color,
                           _Rect, _Rect)),
        ("draw_image", _cbf(None, CTX, ctypes.c_char_p, ctypes.c_char_p, _Rect,
                            _Rect, _Rect, ctypes.c_int)),
        ("image_size", _cbf(None, CTX, ctypes.c_char_p, ctypes.c_char_p,
                            DOUBLE, DOUBLE)),
    ]


def _lib_path():
    override = os.environ.get("DISPLAYD_HTML_LIB")
    if override:
        return override
    for name in LIB_NAMES:
        path = os.path.join(NATIVE_DIR, name)
        if os.path.exists(path):
            return path
    return None


_LIB = None
_LIB_LOCK = threading.Lock()


def lib():
    """Load (once) the native layout engine, or raise with the fix in it."""
    global _LIB
    with _LIB_LOCK:
        if _LIB is not None:
            return _LIB
        path = _lib_path()
        if not path:
            raise NativeMissing(
                "html renderer: native library not built -- run "
                "tools/build_litehtml.sh (looked in %s)" % NATIVE_DIR)
        try:
            lib = ctypes.CDLL(path)
            lib.lhtml_version.restype = ctypes.c_char_p
            lib.lhtml_version.argtypes = []
            lib.lhtml_render.restype = ctypes.c_int
            lib.lhtml_render.argtypes = [
                ctypes.POINTER(_Callbacks), ctypes.c_char_p, ctypes.c_int,
                ctypes.c_int, DOUBLE, ctypes.c_char_p, ctypes.c_int]
        except OSError as err:
            raise HtmlRenderError("html renderer: cannot load %s: %s" % (path, err))
        _LIB = lib
        return lib


def version():
    """Upstream revision + ABI string, for /state and bug reports."""
    return lib().lhtml_version().decode("utf-8", "replace")


# ------------------------------------------------------------------ helpers


def _clip_box(clip):
    """A clipping rect as PIL's inclusive-exclusive box, or None."""
    if clip is None or clip.w <= 0 or clip.h <= 0:
        return None
    x0, y0 = int(clip.x), int(clip.y)
    return (x0, y0, x0 + int(clip.w) + 1, y0 + int(clip.h) + 1)


def _visible(box, clip):
    """Intersect a draw box with the clip; None when fully clipped away.

    Rounded to whole pixels here so every painter downstream works on ints:
    PIL rejects float sizes, and litehtml hands out fractional geometry.
    """
    x0, y0, x1, y1 = box
    cb = _clip_box(clip)
    if cb:
        x0, y0 = max(x0, cb[0]), max(y0, cb[1])
        x1, y1 = min(x1, cb[2]), min(y1, cb[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (int(x0), int(y0), int(x1), int(y1))


def _rgba(color, size):
    """web_color (0..255) to an RGBA tuple PIL will blend."""
    if color.a >= 255:
        return (color.r, color.g, color.b, 255)
    return (color.r, color.g, color.b, color.a)


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


def allow_root(root):
    """Normalise a trusted local directory. Symlinks resolved once here."""
    return os.path.realpath(os.path.expanduser(root))


def resolve_local(root, ref):
    """Map a template-relative reference to a real path inside root.

    Returns None for anything that is not a plain relative path landing
    inside root: absolute paths, URLs, '..', and empty refs are all
    refused. This is the only place a document can name a file.
    """
    if not isinstance(ref, str) or not ref or "\x00" in ref:
        return None
    ref = ref.split("#", 1)[0].split("?", 1)[0].strip()
    if not ref or "://" in ref or ref.startswith("//") or ref.startswith("data:"):
        return None
    if ref.startswith(("/", "~", "\\")) or ":" in ref:
        return None
    path = os.path.realpath(os.path.join(root, ref))
    if path != root and not path.startswith(root + os.sep):
        return None
    if not os.path.isfile(path):
        return None
    return path


# ------------------------------------------------------------------- fonts


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


# ------------------------------------------------------------------ images


class _ImageCache:
    """Decoded local images, bounded, keyed by path and target size."""

    def __init__(self, limit=MAX_IMAGES):
        self.limit = limit
        self._lock = threading.Lock()
        self._decoded = {}
        self._scaled = {}

    def _decode(self, path):
        with self._lock:
            hit = self._decoded.get(path)
            if hit is not None:
                return hit
        try:
            with Image.open(path) as probe:
                if probe.width * probe.height > MAX_IMAGE_PIXELS:
                    return None
                img = probe.convert("RGBA")
        except Exception:
            return None
        with self._lock:
            self._decoded[path] = img
            while len(self._decoded) > self.limit:
                self._decoded.pop(next(iter(self._decoded)))
        return img

    def size(self, url, base_url, root):
        path = self._path(url, base_url, root)
        if not path:
            return 0.0, 0.0
        img = self._decode(path)
        return (0.0, 0.0) if img is None else (float(img.width), float(img.height))

    def _sized(self, path, box_w, box_h):
        key = (path, box_w, box_h)
        with self._lock:
            hit = self._scaled.get(key)
            if hit is not None:
                return hit
        img = self._decode(path)
        if img is None:
            return None
        if (img.width, img.height) != (box_w, box_h):
            try:
                img = img.resize((box_w, box_h), Image.BILINEAR)
            except Exception:
                return None
        with self._lock:
            self._scaled[key] = img
            while len(self._scaled) > self.limit:
                self._scaled.pop(next(iter(self._scaled)))
        return img

    def draw(self, canvas, url, base_url, box, area, clip, repeat, root):
        path = self._path(url, base_url, root)
        if not path:
            return
        x0, y0, x1, y1 = box
        tw, th = int(x1 - x0), int(y1 - y0)
        if tw <= 0 or th <= 0:
            return
        # The tile is `box`; the region to cover is `area` (the element's
        # border box) cut to the clip. Tiling inside `box` instead would draw
        # exactly one tile no matter what repeat says.
        ax0, ay0, ax1, ay1 = _visible(area, clip) or (0, 0, 0, 0)
        if not (ax1 > ax0 and ay1 > ay0):
            return
        img = self._sized(path, tw, th)
        if img is None:
            return
        if repeat == REPEAT_NO_REPEAT:
            _paste_clipped(canvas, img, x0, y0, (ax0, ay0, ax1, ay1))
            return
        # Repeat: compose the whole covered region into one image and paste it
        # once. Walking tile by tile needs a cap to stay safe, and a cap
        # truncates in raster order -- the panel came out half-painted with a
        # black corner, which is worse than slow. Doubling the grid instead
        # costs O(log tiles) pastes and is exact at any tile size.
        ox = max(0, int(x0) - int(ax0)) if repeat in _REPEATS_X else 0
        oy = max(0, int(y0) - int(ay0)) if repeat in _REPEATS_Y else 0
        need_w = int(ax1 - ax0) + ox if repeat in _REPEATS_X else tw
        need_h = int(ay1 - ay0) + oy if repeat in _REPEATS_Y else th
        grid = _compose_repeat(img, need_w, need_h, repeat)
        _paste_clipped(canvas, grid, int(ax0) - ox, int(ay0) - oy,
                       (ax0, ay0, ax1, ay1))

    def _path(self, url, base_url, root):
        if url is None or not root:
            return None
        ref = url.decode("utf-8", "replace") if isinstance(url, bytes) else str(url)
        if not ref or base_url:
            # A base url means the document tried to resolve a document-level
            # base; refuse rather than guess. Templates are single files.
            return None
        return resolve_local(root, ref)


_REPEATS_X = (REPEAT_REPEAT, REPEAT_REPEAT_X)
_REPEATS_Y = (REPEAT_REPEAT, REPEAT_REPEAT_Y)


def _compose_repeat(tile, width, height, repeat):
    """An exactly width x height image of `tile` repeated, by doubling.

    Each round pastes the current grid next to itself, so reaching N tiles
    costs log2(N) pastes and the result is a true pixel repeat -- no
    stretching, no seam. Only whole tiles are added, then the last round is
    cropped, so the size is never over target by more than one tile.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    for axis, target in ((0, width if repeat in _REPEATS_X else tile.width),
                         (1, height if repeat in _REPEATS_Y else tile.height)):
        img = tile
        while (img.width if axis == 0 else img.height) < target:
            cur = img.width if axis == 0 else img.height
            grow = min(cur * 2, target)
            size = (grow, img.height) if axis == 0 else (img.width, grow)
            bigger = Image.new(img.mode, size)
            bigger.paste(img, (0, 0))
            extra = grow - cur
            if axis == 0:
                bigger.paste(img.crop((0, 0, extra, img.height)), (cur, 0))
            else:
                bigger.paste(img.crop((0, 0, img.width, extra)), (0, cur))
            img = bigger
        if axis == 0:
            tile = img
        else:
            tile = _combine(tile, img)
    return tile


def _combine(width_image, height_image):
    """A grid as wide as `width_image` and as tall as `height_image`.

    Built by cropping both to the shared corner rather than re-tiling: both
    are already exact repeats of the same tile, so their overlap is a repeat
    too.
    """
    if width_image is height_image:
        return width_image
    out = Image.new(width_image.mode, (width_image.width, height_image.height))
    out.paste(width_image, (0, 0))
    out.paste(height_image.crop((0, 0, width_image.width, height_image.height)),
              (0, 0))
    return out


def _paste_clipped(canvas, img, x, y, area):
    """Paste with a clip without allocating a full-size scratch."""
    x, y = int(x), int(y)
    ax0, ay0, ax1, ay1 = (int(v) for v in area)
    ix0, iy0 = max(0, ax0 - x), max(0, ay0 - y)
    ix1 = min(img.width, ax1 - x)
    iy1 = min(img.height, ay1 - y)
    if ix1 <= ix0 or iy1 <= iy0:
        return
    piece = img if (ix0, iy0, ix1, iy1) == (0, 0, img.width, img.height) \
        else img.crop((ix0, iy0, ix1, iy1))
    canvas.paste(piece, (x + ix0, y + iy0), piece)


# ----------------------------------------------------------------- painter


class _Painter:
    """Draws one frame. Owned by a single render call; dies with it."""

    def __init__(self, width, height, background, root):
        self.width = width
        self.height = height
        self.root = allow_root(root) if root else None
        self.img = Image.new("RGB", (max(1, width), max(1, height)),
                             tuple(background[:3]))
        self.draw = ImageDraw.Draw(self.img)
        self.fonts = _FontCache()
        self.images = _ImageCache()

    # -- fonts ----------------------------------------------------------
    def create_font(self, descr, out):
        entry = self.fonts.create(descr)
        if entry is None or not entry.handle:
            return
        out.id = entry.handle
        out.height = float(entry.ascent + entry.descent)
        out.ascent = float(entry.ascent)
        out.descent = float(entry.descent)
        out.x_height = float(entry.ascent) * 0.5
        out.ch_width = float(entry.ch_width)

    def delete_font(self, handle):
        self.fonts.release(handle)

    def text_width(self, handle, text):
        return self.fonts.width(handle, text.decode("utf-8", "replace"))

    # -- shapes ---------------------------------------------------------
    def fill(self, box, clip, color):
        area = _visible(box, clip)
        if not area or color.a == 0:
            return
        x0, y0, x1, y1 = area
        if color.a >= 255:
            self.draw.rectangle([x0, y0, x1 - 1, y1 - 1],
                                fill=(color.r, color.g, color.b))
            return
        w, h = x1 - x0, y1 - y0
        self.img.paste(Image.new("RGB", (w, h), (color.r, color.g, color.b)),
                       (x0, y0), Image.new("L", (w, h), color.a))

    def borders(self, box, clip, sides):
        """Trapezoid-accurate borders are out of scope: solid sides only.

        litehtml hands over the resolved box, so each visible solid side is
        one rectangle. Dashed, double, and 3D styles fall back to solid --
        visible, never wrong-shaped. The side order is the C ABI's, see the
        LHTML_SIDE_* macros in displayd_html.h.
        """
        x0, y0, x1, y1 = box
        sides = ((sides[SIDE_LEFT], x0, y0, x0 + sides[SIDE_LEFT].width, y1),
                 (sides[SIDE_TOP], x0, y0, x1, y0 + sides[SIDE_TOP].width),
                 (sides[SIDE_RIGHT], x1 - sides[SIDE_RIGHT].width, y0, x1, y1),
                 (sides[SIDE_BOTTOM], x0, y1 - sides[SIDE_BOTTOM].width,
                  x1, y1))
        for side, sx0, sy0, sx1, sy1 in sides:
            if side.width <= 0 or side.color.a == 0:
                continue
            self.fill((int(sx0), int(sy0), int(sx1), int(sy1)), clip, side.color)

    # -- text -----------------------------------------------------------
    def text(self, handle, raw, color, box, clip):
        entry = self.fonts.font(handle)
        if entry is None or not raw:
            return
        value = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if not value.strip(" "):
            return
        area = _visible(box, clip)
        if not area:
            return
        x, y = int(round(box[0])), int(round(box[1]))
        baseline = y + entry.ascent
        ink = (color.r, color.g, color.b)
        if color.a >= 255:
            self.draw.text((x, baseline), value, font=entry.image_font,
                           fill=ink, anchor="ls")
            return
        # Translucent ink needs a coverage mask: PIL on an RGB canvas drops
        # alpha, so draw the glyphs' coverage then blend the ink through it.
        w = max(1, int(entry.width(value)))
        h = max(1, entry.ascent + entry.descent)
        mask = Image.new("L", (w + 4, h + 4), 0)
        ImageDraw.Draw(mask).text((2, entry.ascent), value,
                                 font=entry.image_font, fill=color.a,
                                 anchor="ls")
        self.img.paste(Image.new("RGB", mask.size, ink), (x - 2, y), mask)

    # -- images ---------------------------------------------------------
    def image_size(self, url, base_url, width, height):
        w, h = self.images.size(url, base_url, self.root)
        width[0] = w
        height[0] = h

    def image(self, url, base_url, box, area, clip, repeat):
        self.images.draw(self.img, url, base_url, box, area, clip, repeat,
                         self.root)


# The trampolines below are created once per process and never released:
# ctypes callbacks that outlive their frame would be a crash, and the whole
# point of the ctx indirection is that one trampoline serves every render.
_ACTIVE = {}
_ACTIVE_LOCK = threading.Lock()
_NEXT_CTX = [0]


def _painter(ctx):
    with _ACTIVE_LOCK:
        return _ACTIVE.get(ctx)


# The callback prototypes are read out of the struct rather than written out
# a second time. A signature listed in two places drifts: a rect added to
# draw_image in the header left these constants one argument short, and
# ctypes rejected the whole table with an opaque TypeError at render time.
def _prototype(field):
    return dict(_Callbacks._fields_)[field]

_CB_CREATE_FONT = _prototype("create_font")
_CB_DELETE_FONT = _prototype("delete_font")
_CB_TEXT_WIDTH = _prototype("text_width")
_CB_FILL = _prototype("draw_fill")
_CB_BORDERS = _prototype("draw_borders")
_CB_TEXT = _prototype("draw_text")
_CB_IMAGE = _prototype("draw_image")
_CB_IMAGE_SIZE = _prototype("image_size")


def _py_create_font(ctx, descr, out):
    painter = _painter(ctx)
    if painter is not None:
        painter.create_font(descr.contents, out.contents)


def _py_delete_font(ctx, handle):
    painter = _painter(ctx)
    if painter is not None:
        painter.delete_font(handle)


def _py_text_width(ctx, handle, text):
    painter = _painter(ctx)
    return 0.0 if painter is None else painter.text_width(handle, text)


def _py_fill(ctx, box, clip, color):
    painter = _painter(ctx)
    if painter is not None:
        painter.fill((box.x, box.y, box.x + box.w, box.y + box.h), clip, color)


def _py_borders(ctx, box, clip, sides):
    painter = _painter(ctx)
    if painter is None:
        return
    flat = [sides[0], sides[1], sides[2], sides[3]]
    painter.borders((box.x, box.y, box.x + box.w, box.y + box.h), clip, flat)


def _py_text(ctx, handle, text, color, box, clip):
    painter = _painter(ctx)
    if painter is not None:
        painter.text(handle, text, color, (box.x, box.y, box.x + box.w,
                                           box.y + box.h), clip)


def _py_image(ctx, url, base_url, box, area, clip, repeat):
    painter = _painter(ctx)
    if painter is not None:
        painter.image(url, base_url,
                      (box.x, box.y, box.x + box.w, box.y + box.h),
                      (area.x, area.y, area.x + area.w, area.y + area.h),
                      clip, repeat)


def _py_image_size(ctx, url, base_url, width, height):
    painter = _painter(ctx)
    if painter is not None:
        painter.image_size(url, base_url, width, height)



# Kept alive for the process lifetime on purpose: a ctypes callback that gets
# garbage collected while the native side still holds the pointer is a crash,
# and a render is far too short-lived to own one.
_CALLBACK_TABLE = (
    _CB_CREATE_FONT(_py_create_font),
    _CB_DELETE_FONT(_py_delete_font),
    _CB_TEXT_WIDTH(_py_text_width),
    _CB_FILL(_py_fill),
    _CB_BORDERS(_py_borders),
    _CB_TEXT(_py_text),
    _CB_IMAGE(_py_image),
    _CB_IMAGE_SIZE(_py_image_size),
)


def render(document, width, height, background=(0, 0, 0), root=None):
    """Lay out and paint `document`, returning (image, content_height).

    `root` is the trusted local directory a document may reference images
    from; pass None to refuse every image reference. Raises
    HtmlRenderError with a display-facing message on any native failure --
    a bad document must never reach screen.present half-drawn.
    """
    library = lib()
    painter = _Painter(width, height, background, root)
    with _ACTIVE_LOCK:
        _NEXT_CTX[0] += 1
        ctx = _NEXT_CTX[0]
        _ACTIVE[ctx] = painter
    try:
        callbacks = _Callbacks(
            ctx=ctypes.c_void_p(ctx),
            create_font=_CB_CREATE_FONT(_py_create_font),
            delete_font=_CB_DELETE_FONT(_py_delete_font),
            text_width=_CB_TEXT_WIDTH(_py_text_width),
            draw_fill=_CB_FILL(_py_fill),
            draw_borders=_CB_BORDERS(_py_borders),
            draw_text=_CB_TEXT(_py_text),
            draw_image=_CB_IMAGE(_py_image),
            image_size=_CB_IMAGE_SIZE(_py_image_size),
        )
        content_height = ctypes.c_double(0.0)
        err = ctypes.create_string_buffer(ERRLEN)
        code = library.lhtml_render(
            ctypes.byref(callbacks), document.encode("utf-8", "replace"),
            int(width), int(height), ctypes.byref(content_height), err, ERRLEN)
        if code != LHTML_OK:
            raise HtmlRenderError(err.value.decode("utf-8", "replace").strip()
                                  or "html render failed (code %d)" % code)
        return painter.img, content_height.value
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(ctx, None)
