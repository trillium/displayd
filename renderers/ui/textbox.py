"""The text-fit ledger: where a line was meant to go vs where it landed.

One declaration path, shared by drawing and checking. A view states the box
a line is meant to stay inside by drawing it through ``ui.text.write`` (its
``room`` / ``box`` arguments) or inside ``boxed(rect)``; this module records
what Pillow was then actually asked to draw -- the measured ink box, not a
re-derived layout -- so the check cannot drift from the renderer: it reads
the same declaration the drawing used.

Recording is off unless ``recording()`` is active on the calling thread (the
text check turns it on for its offscreen probes only), so the live panel
pays one thread-local lookup per line. The Pillow hook is installed once, on
first use, and is a no-op for every thread that is not recording.

What counts as escaping (``violations``), in one place:

- ``outside-frame``  any ink beyond the image the line was drawn on;
- ``wider`` / ``taller``  ink larger than its declared box;
- ``outside-box``  ink inside the box's size but pushed across an edge by
  its origin (names the side: left/top/right/bottom);
- ``cut``  a source string trimmed where the draw declared ``cut=False``;
- ``collapsed``  non-empty text reduced to nothing by a fit.

A box edge of ``None`` is unbounded (a line given only a ``room`` is bound
horizontally); ``SLACK`` px absorbs antialiasing / hinting, nothing more.
"""

import contextlib
import threading

SLACK = 1
SIDES = ("left", "top", "right", "bottom")

_state = threading.local()
_installed = False
_install_lock = threading.Lock()


def active():
    return getattr(_state, "ledger", None) is not None


@contextlib.contextmanager
def recording():
    """Collect every line drawn on this thread; yields the entry list."""
    install()
    previous = getattr(_state, "ledger", None)
    _state.ledger = []
    _state.stack = []
    _state.note = None
    try:
        yield _state.ledger
    finally:
        _state.ledger = previous


@contextlib.contextmanager
def boxed(rect, label=None, cut=True):
    """Declare ``rect`` (l, t, r, b) as the box for lines drawn inside."""
    stack = getattr(_state, "stack", None)
    if stack is None or not active():
        yield
        return
    stack.append((tuple(rect), label, bool(cut)))
    try:
        yield
    finally:
        stack.pop()


def note(source, box=None, cut=True, label=None):
    """Declare the next line: its untrimmed source and intended box."""
    if active():
        _state.note = {"source": source, "box": box, "cut": cut,
                       "label": label}


def derive(xy, room, anchor=None):
    """The horizontal box a ``room`` implies at ``xy`` (vertical unbounded)."""
    try:
        x, room = int(xy[0]), int(room)
    except (TypeError, ValueError, IndexError):
        return None
    if room <= 0:
        return None
    mode = (anchor or "la")[:1]
    left = x - room // 2 if mode == "m" else (x - room if mode == "r" else x)
    return (left, None, left + room, None)


def _entry(draw, xy, text, font, anchor, kwargs):
    note_ = getattr(_state, "note", None)
    _state.note = None
    ink = draw.textbbox(xy, text, font=font, anchor=anchor,
                        **{k: v for k, v in kwargs.items()
                           if k in ("stroke_width", "spacing", "align")})
    size = getattr(draw, "im", None) and draw.im.size
    entry = {"text": text, "source": text, "ink": tuple(int(v) for v in ink),
             "frame": tuple(size) if size else None, "box": None,
             "cut": True, "label": None}
    stack = getattr(_state, "stack", None)
    if stack:
        entry["box"], entry["label"], entry["cut"] = stack[-1]
    if note_:
        entry["source"] = note_["source"]
        entry["cut"] = note_["cut"]
        entry["label"] = note_["label"] or entry["label"]
        if note_["box"] is not None:
            entry["box"] = tuple(note_["box"])
    return entry


def install():
    """Hook ``ImageDraw.text`` once; recording threads only are affected."""
    global _installed
    with _install_lock:
        if _installed:
            return
        import inspect
        from PIL import ImageDraw
        original = ImageDraw.ImageDraw.text
        signature = inspect.signature(original)

        def text(self, xy, text, *args, **kwargs):
            if active() and str(text or ""):
                try:
                    given = signature.bind(self, xy, text, *args,
                                           **kwargs).arguments
                    extra = dict(given.pop("kwargs", {}) or {})
                    extra.update({k: v for k, v in given.items()
                                  if k not in ("self", "xy", "text")})
                    _state.ledger.append(_entry(
                        self, xy, str(text), extra.get("font"),
                        extra.get("anchor"), extra))
                except Exception:
                    _state.note = None
            return original(self, xy, text, *args, **kwargs)

        ImageDraw.ImageDraw.text = text
        _installed = True


def violations(entry):
    """Every way ``entry`` escapes its declaration; ``[]`` when it stays."""
    out = []
    left, top, right, bottom = entry["ink"]
    frame = entry.get("frame")
    if frame:
        edges = (left < -SLACK, top < -SLACK, right > frame[0] + SLACK,
                 bottom > frame[1] + SLACK)
        out.extend({"rule": "outside-frame", "side": SIDES[i]}
                   for i, hit in enumerate(edges) if hit)
    box = entry.get("box")
    if box:
        ink_w, ink_h = right - left, bottom - top
        sides = (box[0], box[1], box[2], box[3])
        if sides[0] is not None and sides[2] is not None \
                and ink_w > sides[2] - sides[0] + SLACK:
            out.append({"rule": "wider", "ink_width": ink_w,
                        "box_width": sides[2] - sides[0]})
        if sides[1] is not None and sides[3] is not None \
                and ink_h > sides[3] - sides[1] + SLACK:
            out.append({"rule": "taller", "ink_height": ink_h,
                        "box_height": sides[3] - sides[1]})
        tests = (sides[0] is not None and left < sides[0] - SLACK,
                 sides[1] is not None and top < sides[1] - SLACK,
                 sides[2] is not None and right > sides[2] + SLACK,
                 sides[3] is not None and bottom > sides[3] + SLACK)
        wide = any(v["rule"] == "wider" for v in out)
        high = any(v["rule"] == "taller" for v in out)
        for i, hit in enumerate(tests):
            if hit and not (wide and i in (0, 2)) and not (high and i in (1, 3)):
                out.append({"rule": "outside-box", "side": SIDES[i]})
    source, drawn = str(entry.get("source") or ""), entry["text"]
    if source.strip() and source != drawn and not entry.get("cut", True):
        out.append({"rule": "cut", "source": source, "drawn": drawn})
    if source.strip() and not drawn.strip():
        out.append({"rule": "collapsed", "source": source})
    return out
