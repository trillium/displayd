"""Frame rendering for the services renderer.

Single concept: draw the cached poll snapshot to the panel. A poll never
blocks a draw, and a cold start (first poll not back yet) renders a
sensible waiting frame -- never blank, never broken.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import ImageDraw, ImageFont

from services_poll import _get_state

C_BG = (8, 8, 12)
C_TEXT = (235, 235, 240)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_UP = (80, 220, 120)
C_DOWN = (130, 130, 140)
C_FAILED = (255, 90, 90)
C_WARN = (255, 180, 60)

PAD = 60
HEADER_SIZE = 72
LABEL_SIZE = 44
COUNT_SIZE = 130
ROW_SIZE = 40
SUB_SIZE = 36
FOOT_SIZE = 30


def _font(screen, name, size):
    try:
        path = screen.font_path(name)
    except Exception:
        return None
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return None


def _font_or_default(screen, name, size):
    return _font(screen, name, size) or ImageFont.load_default()


def _fit(draw, text, font, max_w, max_chars=90):
    text = str(text or "")
    if font is not None:
        try:
            while len(text) > 4 and draw.textlength(text, font=font) > max_w:
                text = text[:-2]
            return text
        except Exception:
            pass
    return text[:max_chars] if len(text) > max_chars else text


def _age(updated):
    if not updated:
        return "no data yet"
    secs = max(0, time.time() - updated)
    if secs < 60:
        return "updated %ds ago" % int(secs)
    if secs < 3600:
        return "updated %dm ago" % int(secs // 60)
    return "updated %dh ago" % int(secs // 3600)


def _short_name(unit):
    for suffix in (".service",):
        if unit.endswith(suffix):
            return unit[: -len(suffix)]
    return unit


def _draw(screen, title, bg, url):
    _, snap, updated, health, error = _get_state()
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font_or_default(screen, "DejaVuSans-Bold", HEADER_SIZE)
    label_font = _font_or_default(screen, "DejaVuSans-Bold", LABEL_SIZE)
    count_font = _font_or_default(screen, "DejaVuSans-Bold", COUNT_SIZE)
    row_font = _font_or_default(screen, "DejaVuSans", ROW_SIZE)
    sub_font = _font_or_default(screen, "DejaVuSans", SUB_SIZE)
    small_font = _font_or_default(screen, "DejaVuSans", FOOT_SIZE)

    host = snap.get("hostname", "") if snap else ""
    header = title + ("  \u00b7  " + host if host else "")
    draw.text((PAD, 24), _fit(draw, header, head_font, screen.W - 2 * PAD),
              font=head_font, fill=C_TEXT)
    dot = {"cold": (120, 120, 130), "warm": C_UP,
           "stale": C_WARN, "error": C_FAILED}[health]
    status = "%s \u00b7 %s" % (health, _age(updated))
    try:
        w = draw.textlength(status, font=small_font)
    except Exception:
        w = 0
    draw.ellipse([screen.W - PAD - 22, 52, screen.W - PAD - 2, 72], fill=dot)
    draw.text((screen.W - PAD - w - 36, 34), status, font=small_font, fill=C_DIM)
    draw.line([(PAD, 128), (screen.W - PAD, 128)], fill=C_LINE, width=2)

    if snap is None:
        # Cold start or a source that has never answered: say so plainly.
        if health == "error":
            big = "SOURCE NOT ANSWERING"
            color = C_FAILED
            sub = "lnx-viz inventory unreachable"
        else:
            big = "waiting for first poll"
            color = C_DIM
            sub = "fetching lnx-viz inventory"
        draw.text((PAD, 220), _fit(draw, big, label_font, screen.W - 2 * PAD),
                  font=label_font, fill=color)
        draw.text((PAD, 300), _fit(draw, sub, row_font, screen.W - 2 * PAD),
                  font=row_font, fill=C_DIM)
        draw.text((PAD, 360), _fit(draw, url, sub_font, screen.W - 2 * PAD),
                  font=sub_font, fill=C_DIM)
        if error:
            draw.text((PAD, 430),
                      _fit(draw, "last error: " + error, sub_font, screen.W - 2 * PAD),
                      font=sub_font, fill=C_FAILED)
        screen.present(img)
        return

    col_w = screen.W - 2 * PAD

    # Tally strip: three glanceable counts.
    tallies = (("UP", snap["up"], C_UP), ("DOWN", snap["down"], C_DOWN),
               ("FAILED", snap["failed"], C_FAILED if snap["failed"] else C_DIM))
    third = col_w / 3.0
    for idx, (label, count, color) in enumerate(tallies):
        x = PAD + idx * third
        draw.text((x, 150), "%d" % count, font=count_font, fill=color)
        draw.text((x + 6, 300), label, font=label_font, fill=C_TEXT)
    draw.text((PAD, 372),
              _fit(draw, "%d services tracked" % snap["total"], sub_font, col_w),
              font=sub_font, fill=C_DIM)
    y = 440
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 26

    # Failed units by name -- the thing the captain actually needs.
    if snap["failed_units"]:
        draw.text((PAD, y), "FAILED", font=label_font, fill=C_FAILED)
        y += 60
        for unit in snap["failed_units"][:3]:
            draw.text((PAD, y), _fit(draw, "\u25cf " + unit, row_font, col_w),
                      font=row_font, fill=C_FAILED)
            y += 56
    else:
        draw.text((PAD, y), "no failed units", font=row_font, fill=C_DIM)
        y += 56

    # Watched services: dots, never a table.
    y += 10
    x = PAD
    for item in snap["watched"][:4]:
        state = item["state"]
        color = C_UP if state == "up" else (C_FAILED if state == "failed" else C_WARN)
        glyph = "\u25cf"
        text = "%s %s" % (glyph, _short_name(item["name"]))
        try:
            tw = draw.textlength(text, font=row_font)
        except Exception:
            tw = len(text) * 20
        if x + tw > screen.W - PAD and x > PAD:
            x = PAD
            y += 56
        draw.text((x, y), text, font=row_font, fill=color)
        x += tw + 60
    y += 62
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 26

    # Containers: one per segment, green when running, amber otherwise.
    if snap["containers"]:
        x = PAD
        try:
            head_w = draw.textlength("CONTAINERS  ", font=row_font)
        except Exception:
            head_w = 0
        draw.text((x, y), "CONTAINERS", font=row_font, fill=C_DIM)
        x += head_w
        for c in snap["containers"][:4]:
            running = c["state"] == "running"
            seg = "%s %s (%s)" % ("\u25cf" if running else "\u25cb",
                                     c["name"], c["state"])
            color = C_UP if running else C_WARN
            try:
                seg_w = draw.textlength(seg + "   ", font=row_font)
            except Exception:
                seg_w = len(seg) * 20
            if x + seg_w > screen.W - PAD and x > PAD + head_w:
                break  # wall space is finite: show fewer, never truncate
            draw.text((x, y), seg, font=row_font, fill=color)
            x += seg_w
        y += 58
    if snap["ports"]:
        # Drop trailing ports until the whole line fits: a cut "pyth"
        # helps nobody, fewer complete entries do.
        shown = list(snap["ports"])
        while shown:
            line = "PORTS  " + "   ".join(
                "%d/%s" % (p["port"], p["process"]) for p in shown)
            try:
                fits = draw.textlength(line, font=sub_font) <= col_w
            except Exception:
                fits = len(line) <= 120
            if fits:
                break
            shown.pop()
        if shown:
            draw.text((PAD, y), line, font=sub_font, fill=C_DIM)
            y += 56

    # Footer: host uptime, and the poll error when stale (honest staleness).
    foot = snap["host_uptime"]
    if health in ("stale", "error") and error:
        foot += "   [inventory poll failed: %s]" % error
    draw.text((PAD, screen.H - 56), _fit(draw, foot, small_font, col_w),
              font=small_font, fill=C_DIM)
    screen.present(img)
