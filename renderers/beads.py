"""Beads parade for the jumbotron: what is moving, stuck, and needs the captain.

Mardi Gras model, copied as a model not code -- four buckets, not a status
column:

    Rolling         in progress, nothing in the way
    Lined Up        open, nothing in the way
    Stalled         waiting on something not done (derived from dependency
                    edges, never read from a status field)
    Past the Stand  closed, folded away

Polled-state archetype: this renderer polls on its own interval in a
background thread and draws from cache. A poll never blocks a draw, a missing
or malformed mirror never kills the frame, and a cold start (first poll not
back yet) renders a sensible waiting frame -- never blank, never broken.

Data: file mirrors first (a deployed snapshot the daemon host cannot build
itself), live `<store> export` as a fallback where the CLIs exist. Both paths
run in the poll thread with timeouts, off the draw path.

Bucket derivation, loading, and the poll cache live in beads_common so the
detail view (beads-detail) cannot drift from this overview.
"""

import os
import sys
import time

from PIL import ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import beads_common as common
from beads_common import (
    BUCKETS,
    C_BG,
    C_DIM,
    C_LINE,
    C_ROLLING,
    C_STALLED,
    C_TEXT,
    DRAW_REFRESH,
    POLL_FALLBACK_INTERVAL,
    _POLL,
    _age,
    _attention,
    _classify,
    _fit,
    _font,
    _load_mirror_file,
)


def _ensure_poll(cfg):
    common.ensure_poll(cfg)


NAME = "beads"
DESCRIPTION = "Beads parade: rolling / lined up / stalled / past the stand, plus what needs the captain"
STATIC = False
PARAMS = {
    "title": {"type": "string", "help": "header text, default BEADS"},
    "mirror": {"type": "string", "help": "mirror file(s), comma-separated JSON/JSONL; auto-discovered when omitted"},
    "stores": {"type": "string", "help": "live fallback stores, comma-separated CLI names; default task,brain,robots,review,ideas; empty disables"},
    "interval": {"type": "integer", "help": "poll seconds, default 60"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}

# ---- drawing -------------------------------------------------------------

PAD = 60
HEADER_SIZE = 72
COUNT_SIZE = 150
BUCKET_LABEL = 44
BUCKET_SUB = 32
ROW_SIZE = 40
FOOT_SIZE = 30


def _draw(screen, title, bg):
    cfg, snap, updated, health, error, source = common.get_state()
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font(screen, "DejaVuSans-Bold", HEADER_SIZE)
    count_font = _font(screen, "DejaVuSans-Bold", COUNT_SIZE)
    lab_font = _font(screen, "DejaVuSans-Bold", BUCKET_LABEL)
    sub_font = _font(screen, "DejaVuSans", BUCKET_SUB)
    row_font = _font(screen, "DejaVuSans", ROW_SIZE)
    small_font = _font(screen, "DejaVuSans", FOOT_SIZE)
    plain = head_font or row_font

    # Header.
    draw.text((PAD, 24), title, font=plain, fill=C_TEXT)
    health_dot = {"cold": (120, 120, 130), "warm": C_ROLLING,
                  "stale": C_STALLED, "error": (255, 90, 90)}[health]
    status = "%s \u00b7 %s" % (health, _age(updated))
    w = 0
    if small_font is not None:
        try:
            w = draw.textlength(status, font=small_font)
        except Exception:
            pass
    draw.ellipse([screen.W - PAD - 22, 52, screen.W - PAD - 2, 72],
                 fill=health_dot)
    draw.text((screen.W - PAD - w - 36, 34), status,
              font=small_font or plain, fill=C_DIM)
    draw.line([(PAD, 128), (screen.W - PAD, 128)], fill=C_LINE, width=2)

    if snap is None:
        # Cold start: a sensible waiting frame, never blank.
        msg = "waiting for first poll \u2014 mirrors or store export"
        draw.text((PAD, 220), _fit(draw, msg, row_font, screen.W - 2 * PAD),
                  font=row_font or plain, fill=C_DIM)
        if error:
            draw.text((PAD, 300),
                      _fit(draw, "last error: " + error, small_font,
                           screen.W - 2 * PAD),
                      font=small_font or plain, fill=(255, 90, 90))
        screen.present(img)
        return

    # Four buckets, one glanceable strip. Big counts shrink to fit their
    # column so a four-digit tally never collides with its neighbour.
    counts = {key: len(snap[key]) for key, _, _, _, _ in BUCKETS}
    col_w = (screen.W - 2 * PAD) / 4.0
    for idx, (key, glyph, label, color, sub) in enumerate(BUCKETS):
        x = PAD + idx * col_w
        text = "%s %d" % (glyph, counts[key])
        font = count_font
        if font is not None:
            try:
                size = COUNT_SIZE
                while (size > 48 and draw.textlength(text, font=font)
                       > col_w - 12):
                    size -= 8
                    font = _font(screen, "DejaVuSans-Bold", size)
                    if font is None:
                        break
            except Exception:
                font = count_font
        draw.text((x, 150), text, font=font or plain, fill=color)
        draw.text((x + 6, 310), label, font=lab_font or plain, fill=C_TEXT)
        draw.text((x + 6, 362), sub, font=sub_font or plain, fill=C_DIM)

    # Running tally + progress.
    total = snap["total"] or 1
    frac = snap["closed"] / total
    bar_y, bar_h = 420, 26
    draw.rectangle([PAD, bar_y, screen.W - PAD, bar_y + bar_h],
                   outline=C_LINE, width=2)
    fill_w = (screen.W - 2 * PAD - 8) * frac
    if fill_w > 2:
        draw.rectangle([PAD + 4, bar_y + 4, PAD + 4 + fill_w,
                        bar_y + bar_h - 4], fill=C_ROLLING)
    tally = "%d closed of %d \u00b7 %d%% past the stand" % (
        snap["closed"], snap["total"], int(frac * 100))
    draw.text((PAD, bar_y + 38), tally, font=sub_font or plain, fill=C_DIM)

    # What needs the captain.
    draw.text((PAD, 500), "NEEDS THE CAPTAIN", font=lab_font or plain,
              fill=C_STALLED)
    y = 562
    for item in snap["attention"]:
        issue, reason = item["issue"], item["reason"]
        # The reason is the actionable part: never truncate it, fit the
        # title into whatever width is left.
        tag = "[%s %s] " % (issue["store"], issue["id"])
        width = screen.W - 2 * PAD - 24  # slack for rasterizer rounding
        # Cap the reason first (~40% of the row), then fit the title
        # into the remainder: the full line always fits the panel.
        reason = _fit(draw, reason, row_font, width * 0.40, max_chars=56)
        tail = " \u2014 " + reason
        title_w = width
        if row_font is not None:
            try:
                fixed = (draw.textlength(tag, font=row_font)
                         + draw.textlength(tail, font=row_font))
                title_w = max(120, width - fixed)
            except Exception:
                pass
        title = _fit(draw, issue["title"], row_font, title_w)
        draw.text((PAD, y), tag + title + tail,
                  font=row_font or plain, fill=C_TEXT)
        y += 72
    if not snap["attention"]:
        draw.text((PAD, y), "nothing waiting on the captain",
                  font=row_font or plain, fill=C_DIM)

    # Footer: per-store open counts + source.
    parts = []
    for store in sorted(snap["stores"]):
        stat = snap["stores"][store]
        parts.append("%s %d/%d" % (store, stat["open"], stat["total"]))
    foot = "  \u00b7  ".join(parts)
    if source:
        foot += "   (%s)" % source
    if health in ("stale", "error") and error:
        foot += "   [last poll failed: %s]" % error
    draw.text((PAD, screen.H - 56),
              _fit(draw, foot, small_font, screen.W - 2 * PAD),
              font=small_font or plain, fill=C_DIM)
    screen.present(img)


def _snapshot_key():
    _, snap, updated, health, _, _ = common.get_state()
    if snap is None:
        return ("cold", health)
    return (updated, len(snap["rolling"]), len(snap["linedup"]),
            len(snap["stalled"]), len(snap["past"]), health)


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or "BEADS").upper()
    bg = screen.color(params.get("background"), C_BG)
    try:
        interval = int(params.get("interval") or POLL_FALLBACK_INTERVAL)
    except (TypeError, ValueError):
        interval = POLL_FALLBACK_INTERVAL
    interval = max(5, min(3600, interval))
    stores = params.get("stores")
    if stores is None:
        stores = "task,brain,robots,review,ideas"
    _ensure_poll({"mirror": params.get("mirror") or "",
                  "stores": stores, "interval": interval})

    # First frame goes up immediately from cache (or the cold frame) --
    # switching here never waits on I/O.
    _draw(screen, title, bg)
    last_key = _snapshot_key()
    last_draw = time.time()
    while not stop.is_set():
        key = _snapshot_key()
        now = time.time()
        if key != last_key or (now - last_draw) >= DRAW_REFRESH:
            last_key = key
            last_draw = now
            try:
                _draw(screen, title, bg)
            except Exception:
                # A draw failure must not kill the daemon loop; the last
                # good frame stays on the panel.
                pass
        stop.wait(1.0)
