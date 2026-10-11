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

Bucket derivation lives in beads_buckets, loading in beads_source, and the
poll cache in beads_poll, so the detail view (beads-detail) cannot drift
from this overview. Shared colours and text fitting live in beads_common.

Store colours and icons live in beads_style plus a JSON config the captain
can edit without touching code. Lookup order (highest first): the
"store_config" renderer param (a path), $DISPLAYD_BEADS_STORES,
<daemon-root>/state/beads-stores.json (survives redeploys), the shipped
renderers/beads_stores.json, then built-in defaults. The file has the shape
{"stores": {"task": {"color": "#6EB4FF", "icon": "\u25cf"}, ...},
"default_icon": "\u25c7"}; only the stores you name need entries, and the
mapping reloads live when the file's mtime changes -- no restart needed.
Unknown stores get a deterministic name-derived colour and the default
icon, so a new store never crashes, never blanks, and never collides with
another unknown store. To add a store, append one entry to
<daemon-root>/state/beads-stores.json. See beads_style for the full contract.
"""

import os
import sys
import time

from PIL import ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import beads_attention as _attention_mod
import beads_layout as layout
from ui import shell as ui_shell
from beads_age import _age
from beads_buckets import _attention, _classify
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
    _fit,
    _font,
    _wrap,
)
from beads_poll import _POLL, ensure_poll, get_state
from beads_source import _load_mirror_file

# Re-exported from beads_attention.py (moved there to keep this file
# within the project's line budget); existing importers keep working.
_text_w = _attention_mod._text_w
draw_lower = _attention_mod.draw_lower


def _ensure_poll(cfg):
    ensure_poll(cfg)


NAME = "beads"
DESCRIPTION = "Beads parade: rolling / lined up / stalled / past the stand, plus what needs the captain"
STATIC = False
PARAMS = {
    "title": {"type": "string", "help": "header text, default BEADS"},
    "mirror": {"type": "string", "help": "mirror file(s), comma-separated JSON/JSONL; auto-discovered when omitted"},
    "stores": {"type": "string", "help": "live fallback stores, comma-separated CLI names; default task,brain,robots,review,ideas; empty disables"},
    "interval": {"type": "integer", "help": "poll seconds, default 60"},
    "store_config": {"type": "string", "help": "store colour/icon JSON path; overrides the shipped mapping, no restart needed"},
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
    cfg, snap, updated, health, error, source = get_state()
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font(screen, "DejaVuSans-Bold", HEADER_SIZE)
    count_font = _font(screen, "DejaVuSans-Bold", COUNT_SIZE)
    lab_font = _font(screen, "DejaVuSans-Bold", BUCKET_LABEL)
    sub_font = _font(screen, "DejaVuSans", BUCKET_SUB)
    row_font = _font(screen, "DejaVuSans", ROW_SIZE)
    small_font = _font(screen, "DejaVuSans", FOOT_SIZE)
    plain = head_font or row_font

    # Header: inset past the badges painted over the top corners.
    hpad = ui_shell.band_pad(screen)
    draw.text((hpad, 24), title, font=plain, fill=C_TEXT)
    health_dot = {"cold": (120, 120, 130), "warm": C_ROLLING,
                  "stale": C_STALLED, "error": (255, 90, 90)}[health]
    status = "%s \u00b7 %s" % (health, _age(updated))
    w = 0
    if small_font is not None:
        try:
            w = draw.textlength(status, font=small_font)
        except Exception:
            pass
    draw.ellipse([screen.W - hpad - 22, 52, screen.W - hpad - 2, 72],
                 fill=health_dot)
    draw.text((screen.W - hpad - w - 36, 34), status,
              font=small_font or plain, fill=C_DIM)
    draw.line([(hpad, 128), (screen.W - hpad, 128)], fill=C_LINE, width=2)

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

    # Four buckets, one glanceable strip, V1 proportional parade: column
    # widths follow share of beads on a square-root scale (floored so
    # Rolling and Stalled stay legible -- see beads_layout). Big counts
    # shrink to fit their own column so a tally never collides with its
    # neighbour.
    counts = [len(snap[key]) for key, _, _, _, _ in BUCKETS]
    widths = layout.column_widths(
        counts, screen.W - 2 * PAD,
        [layout.FLOORS[key] for key, _, _, _, _ in BUCKETS])
    # Narrow floored columns cannot take full-size captions: step the
    # label down and wrap the sub (V1 mockup treatment), so a caption
    # never silently loses letters the way a bare _fit would cut it.
    lab_font_narrow = _font(screen, "DejaVuSans-Bold", 32)
    sub_font_narrow = _font(screen, "DejaVuSans", 26)
    x = float(PAD)
    for (key, glyph, label, color, sub), col_w in zip(BUCKETS, widths):
        text = "%s %d" % (glyph, len(snap[key]))
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
        narrow = col_w < 300
        lf = lab_font_narrow if narrow and lab_font_narrow else lab_font
        sf = sub_font_narrow if narrow and sub_font_narrow else sub_font
        draw.text((x + 6, 310),
                  _fit(draw, label, lf, col_w - 12),
                  font=lf or plain, fill=C_TEXT)
        if narrow:
            for row_i, row in enumerate(
                    _wrap(draw, sub, sf, col_w - 12, 2)):
                # Tighter leading than the single-line baseline: the
                # second wrapped row must clear the progress bar at 420.
                draw.text((x + 6, 356 + row_i * 30), row,
                          font=sf or plain, fill=C_DIM)
        else:
            draw.text((x + 6, 362), _fit(draw, sub, sf, col_w - 12),
                      font=sf or plain, fill=C_DIM)
        x += col_w

    _attention_mod.draw_lower(
        draw, screen, snap, cfg, source=source, health=health, error=error,
        pad=PAD, lab_font=lab_font, row_font=row_font, sub_font=sub_font,
        small_font=small_font, plain=plain)
    screen.present(img)


def _snapshot_key():
    _, snap, updated, health, _, _ = get_state()
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
                  "stores": stores, "interval": interval,
                  "store_config": params.get("store_config") or ""})

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
