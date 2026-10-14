"""The ctypes side of the html renderer: error types, the C ABI structs and
callback table, and the load of the native layout engine.

Everything here mirrors ``renderers/native/displayd_html.h``; nothing paints.
Split out of ``_html_native`` (which re-exports it) to keep each module in
the line budget.
"""

import ctypes
import threading

LHTML_OK = 0
ERRLEN = 240

REPEAT_NO_REPEAT = 0
REPEAT_REPEAT_X = 1
REPEAT_REPEAT_Y = 2
REPEAT_REPEAT = 3


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


class _Radii(ctypes.Structure):
    # Field order and names mirror lhtml_radii in displayd_html.h. ctypes
    # gives C layout, so this is the same eight doubles the container wrote.
    _fields_ = [("top_left_x", ctypes.c_double),
                ("top_left_y", ctypes.c_double),
                ("top_right_x", ctypes.c_double),
                ("top_right_y", ctypes.c_double),
                ("bottom_right_x", ctypes.c_double),
                ("bottom_right_y", ctypes.c_double),
                ("bottom_left_x", ctypes.c_double),
                ("bottom_left_y", ctypes.c_double)]


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
RADII = ctypes.POINTER(_Radii)
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
        ("draw_fill", _cbf(None, CTX, _Rect, _Rect, _Color, RADII)),
        ("draw_borders", _cbf(None, CTX, _Rect, _Rect, SIDES, RADII)),
        ("draw_text", _cbf(None, CTX, ctypes.c_size_t, ctypes.c_char_p, _Color,
                           _Rect, _Rect)),
        ("draw_image", _cbf(None, CTX, ctypes.c_char_p, ctypes.c_char_p, _Rect,
                            _Rect, _Rect, ctypes.c_int)),
        ("image_size", _cbf(None, CTX, ctypes.c_char_p, ctypes.c_char_p,
                            DOUBLE, DOUBLE)),
    ]


_LIB = None
_LIB_LOCK = threading.Lock()


def load(path, searched):
    """Load (once) the native layout engine at `path`, or raise with the fix in it.

    `searched` names where the caller looked, for the missing-library message.
    """
    global _LIB
    with _LIB_LOCK:
        if _LIB is not None:
            return _LIB
        if not path:
            raise NativeMissing(
                "html renderer: native library not built -- run "
                "tools/build_litehtml.sh (looked in %s)" % searched)
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
