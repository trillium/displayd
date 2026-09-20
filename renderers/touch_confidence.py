"""Touchscreen confidence mode for the lnx-server jumbotron.

Opt-in, visible tap-test view: it draws every configured touch region as a
labelled box (id + action), plus prominent title/instructions, so an
operator on the panel can see exactly where to tap. Live tap diagnostics
arrive through POST /feed/touch_confidence/tap (published best-effort by
touch.py when its confidence_feedback switch is on): last tap coordinates,
matched region/action or dead-zone result, total/hit/miss counters, and the
action result/error. Every fed tap changes the frame.

Regions come from the selection params (mirroring touch.json), NOT from a
feed: POST /show {"renderer": "touch_confidence", "params": {"regions":
[...]}}. Each region is {"id": str, "rect": [x, y, w, h], "action": str or
{"name": str}}. Malformed entries are skipped, never fatal -- a half-edited
config still draws the good boxes.

Isolated by design: this renderer reads its own tap feed only and never
touches the policy clock, the playlist, or any other view's buffer.
"""

import time

from PIL import ImageDraw, ImageFont

NAME = "touch_confidence"
DESCRIPTION = ("Touchscreen confidence: region map + live tap diagnostics "
               "(feed POST /feed/touch_confidence/tap)")
STATIC = False
ACCENT = "#50DC78"
PARAMS = {
    "title": {"type": "string",
              "help": "header text, default TOUCH CONFIDENCE"},
    "instructions": {"type": "string",
                     "help": "sub-header, default 'tap a zone, watch it register'"},
    "regions": {"type": "array",
                "help": "touch regions to draw: [{id, rect: [x, y, w, h], "
                        "action}] in display pixels; malformed entries skipped"},
    "width": {"type": "integer",
              "help": "coordinate width the region rects are written in; "
                      "default is the screen width (no scaling)"},
    "height": {"type": "integer",
               "help": "coordinate height the region rects are written in; "
                       "default is the screen height (no scaling)"},
    "background": {"type": "string",
                   "help": "background colour, default near-black"},
}
INPUTS = {
    "tap": {
        "type": "object",
        "help": "one resolved tap: {x, y, region?, action?, hit?, ...}",
        "required": ["x", "y"],
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "x_norm": {"type": "number"},
            "y_norm": {"type": "number"},
            "region": {"type": "string"},
            "action": {"type": "string"},
            "hit": {"type": "boolean"},
            "result": {"type": "string"},
            "error": {"type": "string"},
            "ts": {"type": "number"},
        },
        "buffer": 200,
    },
}

POLL = 0.2  # seconds between buffer checks; draws happen only on change

DEFAULT_TITLE = "TOUCH CONFIDENCE"
DEFAULT_INSTRUCTIONS = "tap a zone \u2014 watch it register here"

# Outline palette cycled per region so adjacent boxes stay distinguishable.
REGION_COLORS = (
    (80, 220, 120),
    (90, 200, 255),
    (255, 200, 90),
    (200, 120, 255),
    (255, 130, 130),
    (120, 220, 220),
)


def _font(screen, name, size):
    try:
        path = screen.font_path(name)
    except Exception:
        return None
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, max(8, int(size)))
    except Exception:
        return None


def format_label(region_id, action):
    """Human text for one box: 'id -> action'. Accepts the touch.json
    action object ({"name": ...}) as well as a plain action-name string."""
    if isinstance(action, dict):
        action = action.get("name")
    return "%s \u2192 %s" % (region_id, action or "no action")


def coerce_regions(params, screen_w, screen_h):
    """Parse the regions param into drawable boxes.

    Returns [(id, (x, y, w, h) in screen px, action)]. Rects are scaled
    from the params width/height coordinate space (default: screen size,
    i.e. no scaling), clipped to the screen, and malformed entries are
    skipped -- never raises on bad user input.
    """
    params = params or {}
    try:
        src_w = int(params.get("width") or screen_w)
        src_h = int(params.get("height") or screen_h)
    except (TypeError, ValueError):
        src_w, src_h = screen_w, screen_h
    if src_w <= 0:
        src_w = screen_w
    if src_h <= 0:
        src_h = screen_h
    raw = params.get("regions")
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for entry in raw:
        try:
            if not isinstance(entry, dict):
                continue
            rid = entry.get("id")
            rect = entry.get("rect")
            if not rid or not isinstance(rid, str):
                continue
            if (not isinstance(rect, (list, tuple)) or len(rect) != 4):
                continue
            x, y, w, h = (float(v) for v in rect)
            if w <= 0 or h <= 0:
                continue
            sx = screen_w / float(src_w)
            sy = screen_h / float(src_h)
            ix, iy = int(round(x * sx)), int(round(y * sy))
            iw, ih = max(1, int(round(w * sx))), max(1, int(round(h * sy)))
            ix = max(0, min(screen_w - 1, ix))
            iy = max(0, min(screen_h - 1, iy))
            iw = max(1, min(screen_w - ix, iw))
            ih = max(1, min(screen_h - iy, ih))
            out.append((rid, (ix, iy, iw, ih), entry.get("action")))
        except (TypeError, ValueError, ArithmeticError):
            continue
    return out


def valid_taps(screen):
    """Buffered tap payloads that carry numeric x/y, oldest first.
    Non-dict or coordinate-less entries are skipped (defensive: the feed
    API normally rejects those, but the renderer must never crash)."""
    taps = []
    try:
        buffered = screen.get_input("touch_confidence", "tap")
    except Exception:
        return []
    for payload in buffered or []:
        if not isinstance(payload, dict):
            continue
        x, y = payload.get("x"), payload.get("y")
        if isinstance(x, bool) or isinstance(y, bool):
            continue
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        taps.append(payload)
    return taps


def summarize(taps):
    """Roll buffered taps into counters. Pure: unit-testable without a screen."""
    total = len(taps)
    hits = 0
    per_region = {}
    for tap in taps:
        rid = tap.get("region") if isinstance(tap, dict) else None
        hit = tap.get("hit") if isinstance(tap, dict) else None
        if hit is None:
            hit = bool(rid)
        if hit:
            hits += 1
            if isinstance(rid, str) and rid:
                per_region[rid] = per_region.get(rid, 0) + 1
    last = taps[-1] if taps else None
    return {"total": total, "hits": hits, "misses": total - hits,
            "per_region": per_region, "last": last}


def _last_line(summary):
    last = summary.get("last")
    if not last:
        return "waiting for taps"
    x, y = last.get("x"), last.get("y")
    rid = last.get("region")
    action = last.get("action")
    if isinstance(action, dict):
        action = action.get("name")
    if rid:
        return "last %s,%s \u2192 %s (%s)" % (x, y, rid, action or "no action")
    return "last %s,%s \u2192 DEAD ZONE \u2014 no action" % (x, y)


def draw(screen, title, instructions, regions, summary, bg):
    """One complete frame. Pure draw (no I/O): tests call this directly."""
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    accent = screen.color(ACCENT, (80, 220, 120))

    # Accent bar across the top: the glanceable bit from across the room.
    draw.rectangle([0, 0, screen.W, max(6, screen.H // 60)], fill=accent)

    title_font = _font(screen, "DejaVuSans-Bold", screen.H // 11)
    sub_font = _font(screen, "DejaVuSans", screen.H // 22)
    box_font = _font(screen, "DejaVuSans-Bold", max(12, screen.H // 30))
    diag_font = _font(screen, "DejaVuSansMono", max(12, screen.H // 32))
    diag_font = diag_font or _font(screen, "DejaVuSans", max(12, screen.H // 32))
    plain = title_font or sub_font or box_font or diag_font

    title_y = int(screen.H * 0.12)
    draw.text((screen.W // 2, title_y), title,
              font=title_font or plain, fill=(255, 255, 255), anchor="mm")
    draw.text((screen.W // 2, title_y + int(screen.H * 0.07)), instructions,
              font=sub_font or plain, fill=(180, 180, 190), anchor="mm")

    # Region map: the middle band. Boxes in config order with labels.
    map_top = int(screen.H * 0.26)
    map_bottom = int(screen.H * 0.66)
    if regions:
        for i, (rid, (x, y, w, h), action) in enumerate(regions):
            color = REGION_COLORS[i % len(REGION_COLORS)]
            # Scale the configured box into the map band vertically so the
            # map always fits on screen regardless of panel geometry.
            frac_top = y / float(max(1, screen.H))
            frac_h = h / float(max(1, screen.H))
            by = map_top + int(frac_top * (map_bottom - map_top))
            bh = max(8, int(frac_h * (map_bottom - map_top)))
            by = max(map_top, min(map_bottom - 8, by))
            bh = max(8, min(map_bottom - by, bh))
            bx, bw = x, w  # horizontal: rects already span the panel width
            draw.rectangle([bx, by, bx + bw - 1, by + bh - 1],
                           outline=color, width=max(2, screen.W // 320))
            label = format_label(rid, action)
            try:
                tw = draw.textlength(label, font=box_font or plain)
                if tw > bw - 12 and tw > 0:
                    label = label[:max(4, int(len(label) * (bw - 12) / tw) - 1)] + "\u2026"
            except Exception:
                pass
            draw.text((bx + 8, by + 6), label,
                      font=box_font or plain, fill=color)
    else:
        draw.text((screen.W // 2, (map_top + map_bottom) // 2),
                  "(no regions configured)",
                  font=sub_font or plain, fill=(140, 140, 150), anchor="mm")

    # Diagnostics: the bottom band. Counters + last tap + result/error.
    dy = int(screen.H * 0.70)
    lh = max(16, int(screen.H * 0.055))
    total, hits, misses = (summary.get("total", 0), summary.get("hits", 0),
                           summary.get("misses", 0))
    draw.text((screen.W // 2, dy), "taps %d    hits %d    miss %d"
              % (total, hits, misses),
              font=diag_font or plain, fill=(255, 255, 255), anchor="mm")
    draw.text((screen.W // 2, dy + lh), _last_line(summary),
              font=diag_font or plain, fill=(255, 255, 160), anchor="mm")
    extra = ""
    last = summary.get("last")
    if last:
        if last.get("error"):
            extra = "error: %s" % last.get("error")
        elif last.get("result"):
            extra = "result: %s" % last.get("result")
    if extra:
        draw.text((screen.W // 2, dy + 2 * lh), extra[:90],
                  font=diag_font or plain, fill=(255, 150, 150), anchor="mm")
    per_region = summary.get("per_region") or {}
    if per_region:
        chips = "   ".join("%s: %d" % (rid, per_region[rid])
                           for rid in sorted(per_region))[:110]
        draw.text((screen.W // 2, dy + (3 if extra else 2) * lh), chips,
                  font=diag_font or plain, fill=(160, 200, 255), anchor="mm")
    return img


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or DEFAULT_TITLE)
    instructions = str(params.get("instructions") or DEFAULT_INSTRUCTIONS)
    bg = screen.color(params.get("background"), (10, 10, 14))
    regions = coerce_regions(params, screen.W, screen.H)

    last_key = None
    while not stop.is_set():
        taps = valid_taps(screen)
        summary = summarize(taps)
        last = summary["last"]
        try:
            last_repr = repr(sorted(last.items())) if last else None
        except Exception:
            last_repr = repr(last)
        key = (summary["total"], last_repr)
        if key != last_key:
            last_key = key
            # One complete frame, one swap: never a partial confidence card.
            screen.present(draw(screen, title, instructions, regions,
                                summary, bg))
        stop.wait(POLL)
