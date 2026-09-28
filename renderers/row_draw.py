"""Frame rendering for the rowing-streak view. Single concept: draw.

Palette, type sizes, font helpers, the waiting/error message frame, the
main streak frame, and the snapshot cache key. Pure presentation: no I/O,
no polling. Imported and re-exported by row.py so existing references
(row_view._draw, row_view.C_BG, ...) keep working.
"""

import time

from PIL import ImageDraw, ImageFont

C_BG = (8, 8, 12)
C_TEXT = (235, 235, 240)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_FIRE = (255, 150, 50)
C_UP = (80, 220, 120)
C_FAILED = (255, 90, 90)
C_WARN = (255, 180, 60)

PAD = 60
HEADER_SIZE = 72
BIG_SIZE = 300
LABEL_SIZE = 44
ROW_SIZE = 48
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


def _draw_message(screen, title, bg, big, sub, foot, color):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font_or_default(screen, "DejaVuSans-Bold", HEADER_SIZE)
    label_font = _font_or_default(screen, "DejaVuSans-Bold", LABEL_SIZE)
    row_font = _font_or_default(screen, "DejaVuSans", ROW_SIZE)
    sub_font = _font_or_default(screen, "DejaVuSans", SUB_SIZE)
    small_font = _font_or_default(screen, "DejaVuSans", FOOT_SIZE)
    draw.text((PAD, 24), _fit(draw, title, head_font, screen.W - 2 * PAD),
              font=head_font, fill=C_TEXT)
    draw.line([(PAD, 128), (screen.W - PAD, 128)], fill=C_LINE, width=2)
    draw.text((PAD, 220), _fit(draw, big, label_font, screen.W - 2 * PAD),
              font=label_font, fill=color)
    if sub:
        draw.text((PAD, 300), _fit(draw, sub, row_font, screen.W - 2 * PAD),
                  font=row_font, fill=C_DIM)
    if foot:
        draw.text((PAD, screen.H - 56),
                  _fit(draw, foot, small_font, screen.W - 2 * PAD),
                  font=small_font, fill=C_DIM)
    screen.present(img)


def _draw(screen, title, bg, snap, label, health, error, updated):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font_or_default(screen, "DejaVuSans-Bold", HEADER_SIZE)
    big_font = _font_or_default(screen, "DejaVuSans-Bold", BIG_SIZE)
    label_font = _font_or_default(screen, "DejaVuSans-Bold", LABEL_SIZE)
    row_font = _font_or_default(screen, "DejaVuSans", ROW_SIZE)
    sub_font = _font_or_default(screen, "DejaVuSans", SUB_SIZE)
    small_font = _font_or_default(screen, "DejaVuSans", FOOT_SIZE)

    dot = {"cold": (120, 120, 130), "warm": C_UP,
           "stale": C_WARN, "error": C_FAILED}[health]
    status = "%s · %s" % (health, _age(updated))
    try:
        w = draw.textlength(status, font=small_font)
    except Exception:
        w = 0
    draw.text((PAD, 24), _fit(draw, title, head_font, screen.W - 2 * PAD - w - 80),
              font=head_font, fill=C_TEXT)
    draw.ellipse([screen.W - PAD - 22, 52, screen.W - PAD - 2, 72], fill=dot)
    draw.text((screen.W - PAD - w - 36, 34), status, font=small_font, fill=C_DIM)
    draw.line([(PAD, 128), (screen.W - PAD, 128)], fill=C_LINE, width=2)

    col_w = screen.W - 2 * PAD
    ds, rs, bank = snap["day_streak"], snap["row_streak"], snap["bank"]

    # Hero: the day streak, with a fire marker while alive.
    hero = "%d" % ds
    try:
        while len(hero) > 1 and draw.textlength(hero, font=big_font) > col_w * 0.55:
            hero_size = big_font.size - 20
            big_font = _font_or_default(screen, "DejaVuSans-Bold", max(60, hero_size))
            if big_font.size <= 60:
                break
    except Exception:
        pass
    hero_color = C_FIRE if ds > 0 else C_DIM
    draw.text((PAD, 150), hero, font=big_font, fill=hero_color)
    try:
        hw = draw.textlength(hero, font=big_font)
    except Exception:
        hw = 0
    draw.text((PAD + hw + 40, 200),
              _fit(draw, "DAY STREAK" if ds != 1 else "DAY STREAK",
                   label_font, col_w - hw - 40),
              font=label_font, fill=C_TEXT)
    draw.text((PAD + hw + 40, 280),
              _fit(draw, "%d rows · bank %d" % (rs, bank), row_font, col_w - hw - 40),
              font=row_font, fill=C_DIM)
    y = 560
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 26

    # Last row + year pace: the glanceable second line.
    last = snap["last_day"] or "—"
    draw.text((PAD, y), _fit(draw, "last row  %s" % last, row_font, col_w),
              font=row_font, fill=C_TEXT)
    y += 70
    pace = snap["pace"]
    if pace > 0:
        pace_text = "year %d/%d · %d ahead of pace" % (
            snap["rows_year"], snap["days_in_year"], pace)
        pace_color = C_UP
    elif pace < 0:
        pace_text = "year %d/%d · %d behind pace" % (
            snap["rows_year"], snap["days_in_year"], -pace)
        pace_color = C_WARN
    else:
        pace_text = "year %d/%d · on pace" % (snap["rows_year"], snap["days_in_year"])
        pace_color = C_TEXT
    draw.text((PAD, y), _fit(draw, pace_text, row_font, col_w),
              font=row_font, fill=pace_color)
    y += 70
    draw.text((PAD, y), _fit(draw, snap["status"], sub_font, col_w),
              font=sub_font, fill=C_DIM)

    foot = label or "no log configured"
    if health == "stale":
        foot += "   [STALE \u2014 last-known streak]"
    if health in ("stale", "error") and error:
        foot += "   [read failed: %s]" % error
    draw.text((PAD, screen.H - 56), _fit(draw, foot, small_font, col_w),
              font=small_font, fill=C_DIM)
    screen.present(img)


def _snapshot_key(snap, health):
    if snap is None:
        return ("cold", health)
    return (snap["day_streak"], snap["row_streak"], snap["bank"],
            snap["last_ts"], snap["rows_year"], health)
