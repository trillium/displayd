"""Per-app window locations for the talon_apps feed (runs on the MacBook).

Sibling of ``bridges/talon_apps.py`` (same stdlib + PyObjC-in-functions
shape, no repo imports so the durable copy stays two files): each tick
the bridge may call :func:`snapshot` to enumerate on-screen windows
(CGWindowList, layer-0 -- bounds and order need no Screen Recording
grant) and attach ``windows`` + ``displays`` to the feed document, so
the panel can group app buttons by the screen they are on.

Derivation (stated plainly, per the brief): an app is placed by the
centre of its FRONTMOST on-screen window (first match in the
front-to-back window list), containment-tested against the display
bounds -- the same approach as ``macos_state``. Multi-display app
(windows on several screens) places by its frontmost window only;
a windowless app (or one with no on-screen window) has no entry and
the panel seats it with the main display's side. Matching is exact
first, casefold second. Never raises: Quartz failures yield (None,
None) and the feed stays a flat list.
"""

MAX_ITEMS = 30


def _num(value):
    return value if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else None


def containing(x, y, displays):
    """Index of the display containing (x, y), else None. Pure."""
    try:
        for i, d in enumerate(displays or []):
            b = d.get("bounds") if isinstance(d, dict) else None
            if not isinstance(b, dict):
                continue
            bx, by = _num(b.get("x")), _num(b.get("y"))
            bw, bh = _num(b.get("w")), _num(b.get("h"))
            if None in (bx, by, bw, bh) or bw <= 0 or bh <= 0:
                continue
            if bx <= x < bx + bw and by <= y < by + bh:
                return i
    except Exception:
        return None
    return None


def place(apps, owners, displays):
    """Feed apps -> bounded {name: {"x","y","d"}} window-centre map.

    `owners` maps window-owner name -> (cx, cy) frontmost centre (or a
    {"x","y"} dict); only feed-listed apps are emitted, at most
    MAX_ITEMS, integer coordinates. Never raises."""
    try:
        names = list(apps) if isinstance(apps, (list, tuple)) else []
    except Exception:
        return {}
    try:
        folded = {str(k).casefold(): k for k in (owners or {})}
    except Exception:
        folded = {}
    out = {}
    for name in names:
        if not isinstance(name, str) or not name or len(out) >= MAX_ITEMS:
            continue
        raw = None
        try:
            if name in (owners or {}):
                raw = owners[name]
            elif name.casefold() in folded:
                raw = owners[folded[name.casefold()]]
        except Exception:
            raw = None
        pt = None
        try:
            if isinstance(raw, dict):
                pt = (_num(raw.get("x")), _num(raw.get("y")))
            elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
                pt = (_num(raw[0]), _num(raw[1]))
        except Exception:
            pt = None
        if not pt or pt[0] is None or pt[1] is None:
            continue
        cx, cy = int(pt[0]), int(pt[1])
        out[name] = {"x": cx, "y": cy,
                     "d": containing(cx, cy, displays)}
    return out


def snapshot():
    """(owners, displays) from one CG pass, or (None, None) when Quartz
    is unavailable. PyObjC imports live here so this module imports
    anywhere (tests, Linux panel) without them."""
    try:
        import Quartz
    except Exception:
        return None, None
    try:
        _, ids, _ = Quartz.CGGetActiveDisplayList(8, None, None)
        main_id = Quartz.CGMainDisplayID()
        boxes = []
        for d in ids or []:
            b = Quartz.CGDisplayBounds(d)
            boxes.append({"bounds": {
                "x": int(b.origin.x), "y": int(b.origin.y),
                "w": int(b.size.width), "h": int(b.size.height)},
                "main": bool(d == main_id)})
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID)
        owners = {}
        for w in info or []:
            try:
                if int(w.get("kCGWindowLayer", 0)) != 0:
                    continue
                owner = w.get("kCGWindowOwnerName")
                if not owner or owner in owners:
                    continue  # front-to-back: first match is frontmost
                b = w.get("kCGWindowBounds") or {}
                x, y = _num(b.get("X")), _num(b.get("Y"))
                bw, bh = _num(b.get("Width")), _num(b.get("Height"))
                if None in (x, y, bw, bh) or bw <= 0 or bh <= 0:
                    continue
                owners[str(owner)] = (x + bw / 2.0, y + bh / 2.0)
            except Exception:
                continue
        return owners, boxes
    except Exception:
        return None, None
