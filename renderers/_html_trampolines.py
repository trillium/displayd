"""The ctypes trampolines between litehtml and the PIL painter.

One set of callbacks serves every render: each call carries a ``ctx`` id that
:func:`register` handed out, and the id picks the painter that owns the frame.
"""

import ctypes
import threading

from _html_abi import _Callbacks
from _html_geom import _radii, _shape_for

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


def _py_fill(ctx, box, clip, color, radii):
    painter = _painter(ctx)
    if painter is None:
        return
    rect = (box.x, box.y, box.x + box.w, box.y + box.h)
    painter.fill(rect, clip, color, _shape_for(rect, clip, radii))


def _py_borders(ctx, box, clip, sides, radii):
    painter = _painter(ctx)
    if painter is None:
        return
    flat = [sides[0], sides[1], sides[2], sides[3]]
    painter.borders((box.x, box.y, box.x + box.w, box.y + box.h), clip,
                    flat, _radii(radii))


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



def register(painter):
    """Give `painter` a ctx id the native side can name it by."""
    with _ACTIVE_LOCK:
        _NEXT_CTX[0] += 1
        ctx = _NEXT_CTX[0]
        _ACTIVE[ctx] = painter
    return ctx


def unregister(ctx):
    with _ACTIVE_LOCK:
        _ACTIVE.pop(ctx, None)


def callbacks(ctx):
    """The callback struct for one render, wired to the shared trampolines."""
    return _Callbacks(
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
