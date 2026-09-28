"""Frame rendering for the resources renderer.

Single concept: draw the cached poll snapshot to the panel. A poll never
blocks a draw, and a cold start (first poll not back yet) renders a
sensible waiting frame -- never blank, never broken.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import ImageDraw, ImageFont

from resources_poll import _get_state

C_BG = (8, 8, 12)
C_TEXT = (235, 235, 240)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_OK = (80, 220, 120)
C_WARN = (255, 180, 60)
C_BAD = (255, 90, 90)

PAD = 60
HEADER_SIZE = 72
LABEL_SIZE = 44
BIG_SIZE = 170
SUB_SIZE = 40
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


# ---- formatting ----------------------------------------------------------

def _gb(n):
    return "%.1f" % (float(n) / (1024 ** 3))


def _uptime(s):
    s = max(0, int(s or 0))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins, _ = divmod(s, 60)
    if days:
        return "up %dd %dh" % (days, hours)
    if hours:
        return "up %dh %dm" % (hours, mins)
    if mins:
        return "up %dm" % mins
    return "up %ds" % s


def _age(updated):
    if not updated:
        return "no data yet"
    secs = max(0, time.time() - updated)
    if secs < 60:
        return "updated %ds ago" % int(secs)
    if secs < 3600:
        return "updated %dm ago" % int(secs // 60)
    return "updated %dh ago" % int(secs // 3600)


# ---- drawing --------------------------------------------------------------

def _bar(draw, x, y, w, h, frac, color):
    draw.rectangle([x, y, x + w, y + h], outline=C_LINE, width=2)
    fill_w = (w - 8) * max(0.0, min(1.0, frac))
    if fill_w > 2:
        draw.rectangle([x + 4, y + 4, x + 4 + fill_w, y + h - 4], fill=color)


def _draw(screen, title, bg):
    _, snap, updated, health, error = _get_state()
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font_or_default(screen, "DejaVuSans-Bold", HEADER_SIZE)
    label_font = _font_or_default(screen, "DejaVuSans-Bold", LABEL_SIZE)
    big_font = _font_or_default(screen, "DejaVuSans-Bold", BIG_SIZE)
    sub_font = _font_or_default(screen, "DejaVuSans", SUB_SIZE)
    small_font = _font_or_default(screen, "DejaVuSans", FOOT_SIZE)

    host = (snap or {}).get("hostname", "") if snap else ""
    header = title + ("  \u00b7  " + host if host else "")
    draw.text((PAD, 24), _fit(draw, header, head_font, screen.W - 2 * PAD),
              font=head_font, fill=C_TEXT)
    dot = {"cold": (120, 120, 130), "warm": C_OK,
           "stale": C_WARN, "error": C_BAD}[health]
    status = "%s \u00b7 %s" % (health, _age(updated))
    try:
        w = draw.textlength(status, font=small_font)
    except Exception:
        w = 0
    draw.ellipse([screen.W - PAD - 22, 52, screen.W - PAD - 2, 72], fill=dot)
    draw.text((screen.W - PAD - w - 36, 34), status, font=small_font, fill=C_DIM)
    draw.line([(PAD, 128), (screen.W - PAD, 128)], fill=C_LINE, width=2)

    if snap is None:
        msg = "waiting for first poll \u2014 reading /proc" if health == "cold" \
            else "no data: last poll failed"
        draw.text((PAD, 260), _fit(draw, msg, label_font, screen.W - 2 * PAD),
                  font=label_font, fill=C_DIM)
        if error:
            draw.text((PAD, 340),
                      _fit(draw, "last error: " + error, sub_font, screen.W - 2 * PAD),
                      font=sub_font, fill=C_BAD)
        screen.present(img)
        return

    col_w = screen.W - 2 * PAD
    y = 170

    # Row 1: CPU -- the giant number. Load matters on this host, so it gets
    # the sub-line in full: 1/5/15 plus core count and thread pressure.
    draw.text((PAD, y), "CPU", font=label_font, fill=C_DIM)
    pct = snap["cpu_pct"]
    if pct is None:
        cpu_big, cpu_color = "sampling\u2026", C_DIM
    else:
        cpu_big = "%d%%" % int(round(pct))
        cpu_color = C_OK if pct < 70 else (C_WARN if pct < 90 else C_BAD)
    draw.text((PAD, y + 30), cpu_big, font=big_font, fill=cpu_color)
    l1, l5, l15 = snap["load"]
    load_line = "load %.2f  %.2f  %.2f   \u00b7   %d cores   \u00b7   %s procs" % (
        l1, l5, l15, snap["ncpu"], snap["procs"])
    draw.text((PAD + 520, y + 110),
              _fit(draw, load_line, sub_font, col_w - 520),
              font=sub_font, fill=C_TEXT)
    y += 250
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 30

    # Row 2: memory + swap.
    draw.text((PAD, y), "MEM", font=label_font, fill=C_DIM)
    mem_line = "%s / %s GB" % (_gb(snap["mem_used"]), _gb(snap["mem_total"]))
    mem_frac = (float(snap["mem_used"]) / snap["mem_total"]) if snap["mem_total"] else 0.0
    draw.text((PAD, y + 30),
              _fit(draw, mem_line, big_font, col_w - 500),
              font=big_font, fill=C_TEXT)
    _bar(draw, PAD + 1300, y + 90, col_w - 1300, 44, mem_frac,
         C_OK if mem_frac < 0.8 else (C_WARN if mem_frac < 0.93 else C_BAD))
    if snap["swap_total"]:
        swap_line = "swap %s / %s GB" % (_gb(snap["swap_used"]), _gb(snap["swap_total"]))
    else:
        swap_line = "no swap"
    draw.text((PAD + 1300, y + 150),
              _fit(draw, swap_line, sub_font, col_w - 1300),
              font=sub_font, fill=C_DIM)
    y += 260
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 30

    # Row 3: disk, one line per mount.
    draw.text((PAD, y), "DISK", font=label_font, fill=C_DIM)
    y += 62
    for disk in snap["disks"][:3]:  # wall space is finite: three mounts max
        if "error" in disk:
            line = "%s: %s" % (disk["mount"], disk["error"])
            draw.text((PAD, y), _fit(draw, line, sub_font, col_w),
                      font=sub_font, fill=C_BAD)
        else:
            frac = disk["pct"] / 100.0
            line = "%s  %s / %s GB  %d%%" % (
                disk["mount"], _gb(disk["used"]), _gb(disk["total"]),
                int(round(disk["pct"])))
            draw.text((PAD, y), _fit(draw, line, sub_font, col_w - 560),
                      font=sub_font, fill=C_TEXT)
            _bar(draw, PAD + col_w - 520, y + 2, 520, 40, frac,
                 C_OK if frac < 0.8 else (C_WARN if frac < 0.92 else C_BAD))
        y += 68

    # Footer: uptime, and the poll error when stale (honest staleness).
    foot = _uptime(snap["uptime_s"])
    if health in ("stale", "error") and error:
        foot += "   [last poll failed: %s]" % error
    draw.text((PAD, screen.H - 56),
              _fit(draw, foot, small_font, col_w),
              font=small_font, fill=C_DIM)
    screen.present(img)
