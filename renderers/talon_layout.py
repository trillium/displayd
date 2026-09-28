"""Side-button geometry for the talon_apps view: which apps sit LEFT/RIGHT.

Pure layout (no PIL, no daemon state): the renderer draws it, the
daemon hit-tests taps against it, and touch regions cover the two
columns -- one source of truth so draw, tap, and region cannot drift.

Grouping: with 2+ displays the LEFT column holds the apps whose
windows are on the leftmost display (min x, then min y of the display
bounds -- derived from the actual arrangement, never a hardcoded
index); every other app sits RIGHT. Unknown/no-window apps join the
main display's side. With 0-1 displays there is no meaningful side,
so the list splits evenly across both columns rather than leaving
one side empty. Never raises; garbage inputs degrade to the split.
"""

PAD = 48
LIST_TOP = 250
ROW_H = 72
COL_W = 560
MAX_PER_SIDE = 9


def _num(value):
    return value if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else None


def _leftmost(displays):
    """Index of the leftmost display (min x, then min y), else None."""
    best, best_key = None, None
    for i, d in enumerate(displays or []):
        b = d.get("bounds") if isinstance(d, dict) else None
        if not isinstance(b, dict):
            continue
        x, y = _num(b.get("x")), _num(b.get("y"))
        if x is None or y is None:
            continue
        if best_key is None or (x, y) < best_key:
            best, best_key = i, (x, y)
    return best


def _main(displays):
    """Index of the marked-main display, else 0 when any exist."""
    for i, d in enumerate(displays or []):
        if isinstance(d, dict) and d.get("main"):
            return i
    return 0 if displays else None


def _side_of(app, windows, leftmost, main):
    """LEFT/RIGHT side for one app name via its window display."""
    disp = None
    try:
        info = (windows or {}).get(app)
        disp = info.get("d") if isinstance(info, dict) else None
    except Exception:
        disp = None
    if disp == leftmost and leftmost is not None:
        return "left"
    if isinstance(disp, int) and disp != leftmost:
        return "right"
    # Unknown or windowless: sit with the main display's side.
    if main == leftmost and leftmost is not None:
        return "left"
    return "right"


def group(apps, windows=None, displays=None):
    """Split app indices into LEFT/RIGHT columns.

    Returns {"left", "right", "overflow", "mode", "left_display"}.
    `apps` is the raw feed list; `windows` maps name -> {"d"} display
    index; `displays` is the feed display list. Capped at
    MAX_PER_SIDE per side; the rest counts as overflow. Never raises.
    """
    try:
        names = list(apps) if isinstance(apps, (list, tuple)) else []
    except Exception:
        names = []
    try:
        disp_list = list(displays) \
            if isinstance(displays, (list, tuple)) else []
    except Exception:
        disp_list = []
    leftmost = _leftmost(disp_list)
    main = _main(disp_list)
    left, right = [], []
    if len(disp_list) >= 2 and leftmost is not None:
        mode = "sides"
        for i in range(len(names)):
            side = _side_of(names[i], windows, leftmost, main)
            (left if side == "left" else right).append(i)
    else:
        mode = "split"
        half = (len(names) + 1) // 2
        left, right = list(range(half)), list(range(half, len(names)))
    overflow = max(0, len(left) - MAX_PER_SIDE) + \
        max(0, len(right) - MAX_PER_SIDE)
    return {"left": left[:MAX_PER_SIDE], "right": right[:MAX_PER_SIDE],
            "overflow": overflow, "mode": mode, "left_display": leftmost}


def button_rect(side, slot, width):
    """Panel rect of one app button: (x, y, w, h)."""
    y = LIST_TOP + slot * ROW_H
    h = ROW_H - 8
    if side == "right":
        return (width - PAD - COL_W, y, COL_W, h)
    return (PAD, y, COL_W, h)


def column_rect(side, width, height):
    """Touch-strip rect covering one button column: [x, y, w, h]."""
    y, h = LIST_TOP, max(0, height - LIST_TOP - PAD)
    if side == "right":
        return [width - PAD - COL_W, y, COL_W, h]
    return [PAD, y, COL_W, h]


def hit(px, py, width, height, groups):
    """App index for a panel tap, or None on a miss. Never raises."""
    try:
        if not isinstance(groups, dict):
            return None
        for side in ("left", "right"):
            slots = groups.get(side) or []
            for slot, index in enumerate(list(slots)):
                x, y, w, h = button_rect(side, slot, width)
                if x <= px < x + w and y <= py < y + h:
                    return int(index)
    except Exception:
        return None
    return None
