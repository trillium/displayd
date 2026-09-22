"""Concept2 rowing streak for the jumbotron: current streak + last row.

File-polled archetype (cf. services.py): this renderer re-reads
row_tracker's `rows.txt` on its own interval and draws from the parsed
snapshot. A read never blocks a draw, a missing or malformed log never
kills the frame, and a cold start renders a sensible waiting frame --
never blank, never broken.

Streak math mirrors `row.sh`'s rest-day bank rule exactly:
  * every row beyond the first on one calendar day deposits one credit;
  * a zero-row day withdraws one credit and the streak carries through
    (a "covered" rest day: day streak grows, row streak unchanged);
  * an empty bank plus a missed day breaks the streak, fresh start at
    1 day on the next row (no inheritance);
  * streaks reset at the year boundary (rows < year never mix).

Log lines are ISO timestamps (`YYYY-MM-DDTHH:MM:SS+HH:MM`); anything
else in the file (blank lines, `??` placeholders) is ignored.
"""

import datetime
import os
import time

from PIL import ImageDraw, ImageFont

NAME = "row"
DESCRIPTION = "Concept2 rowing streak: current day/row streak, last row, year pace (from row_tracker rows.txt)"
STATIC = False
# Playlist progress-bar colour for this view (see playlist.accent_for).
ACCENT = "#5CFF9D"
PARAMS = {
    "path": {"type": "string", "help": "rows.txt path; default $DISPLAYD_ROW_FILE, else sibling row_tracker checkout"},
    "title": {"type": "string", "help": "header text, default ROWING"},
    "interval": {"type": "integer", "help": "poll seconds, default 60"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}

ENV_VAR = "DISPLAYD_ROW_FILE"
POLL_DEFAULT_INTERVAL = 60
DRAW_REFRESH = 60  # re-render at least this often so the age line stays honest

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


# ---- path resolution (no hardcoded home paths) ---------------------------

def resolve_path(explicit=None):
    """Where to read the row log from. Precedence: explicit `path`
    param, `$DISPLAYD_ROW_FILE`, then a sibling `row_tracker/rows.txt`
    next to (up to three levels above) this displayd checkout -- so the
    pairing survives checkout moves on any host. Returns None when no
    candidate exists; the panel says so instead of going blank."""
    if explicit:
        return str(explicit)
    env = os.environ.get(ENV_VAR)
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))  # .../displayd/renderers
    root = os.path.dirname(here)  # .../displayd
    node = root
    for _ in range(4):
        cand = os.path.join(os.path.dirname(node), "row_tracker", "rows.txt")
        if os.path.exists(cand):
            return cand
        direct = os.path.join(node, "row_tracker", "rows.txt")
        if os.path.exists(direct):
            return direct
        node = os.path.dirname(node)
    return None


# ---- streak math (pure over parsed log: unit-testable) --------------------

def parse_log(text):
    """Fold raw rows.txt text into ({date: rows-that-day}, last timestamp
    string or None, total row count). Non-timestamp lines are ignored."""
    counts = {}
    last_ts = None
    total = 0
    for line in str(text or "").splitlines():
        s = line.strip()
        if len(s) < 10 or not s[0].isdigit():
            continue
        try:
            day = datetime.date.fromisoformat(s[:10])
        except ValueError:
            continue
        counts[day] = counts.get(day, 0) + 1
        total += 1
        if last_ts is None or s > last_ts:
            last_ts = s
    return counts, last_ts, total


def _step(state, count):
    """One calendar day through the rest-day bank rule. `state` is
    [day_streak, row_streak, bank]; mirrors row.sh `_streak_step`."""
    ds, rs, bank = state
    if count > 0:
        if ds == 0:
            state[0], state[1], state[2] = 1, count, count - 1
        else:
            state[0], state[1], state[2] = ds + 1, rs + count, bank + count - 1
    elif ds > 0 and bank > 0:
        # Covered rest day: spend one credit, streak holds. Day streak
        # grows (calendar days), row streak unchanged, bank drops by 1.
        state[0], state[2] = ds + 1, bank - 1
    else:
        state[0], state[1], state[2] = 0, 0, 0


def compute_streaks(counts, as_of):
    """Walk the per-day counts forward to `as_of` (a date, inclusive)
    and return (day_streak, row_streak, bank). Future days are ignored;
    streaks reset at the year boundary. Matches row.sh compute_streaks."""
    state = [0, 0, 0]
    past = sorted((d, c) for d, c in counts.items() if d <= as_of)
    year = None
    prev = None
    for day, count in past:
        if prev is not None:
            gap = prev + datetime.timedelta(days=1)
            while gap < day:
                if gap.year != year:
                    state[0] = state[1] = state[2] = 0
                    year = gap.year
                _step(state, 0)
                gap += datetime.timedelta(days=1)
        if day.year != year:
            state[0] = state[1] = state[2] = 0
            year = day.year
        _step(state, count)
        prev = day
    if prev is not None:
        gap = prev + datetime.timedelta(days=1)
        while gap <= as_of:
            if gap.year != year:
                state[0] = state[1] = state[2] = 0
                year = gap.year
            _step(state, 0)
            gap += datetime.timedelta(days=1)
    return tuple(state)


def summarize(counts, last_ts, total, as_of=None):
    """Shape parsed log data into what the wall needs."""
    today = as_of or datetime.date.today()
    ds, rs, bank = compute_streaks(counts, today)
    year = today.year
    rows_year = sum(c for d, c in counts.items() if d.year == year)
    day_of_year = today.timetuple().tm_yday
    days_in_year = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    last_day = None
    if last_ts:
        try:
            last_day = datetime.date.fromisoformat(last_ts[:10])
        except ValueError:
            last_day = None
    if not counts or (last_day is not None and last_day.year != year and rows_year == 0):
        status = "no rows yet" if not counts else "season not started"
    elif ds == 0:
        status = "streak broken"
    elif last_day == today:
        status = "rowed today"
    else:
        status = "rest — streak held" if bank >= 0 and counts.get(today, 0) == 0 else "rowed today"
        if counts.get(today, 0) > 0:
            status = "rowed today"
    return {
        "day_streak": ds,
        "row_streak": rs,
        "bank": bank,
        "last_ts": last_ts,
        "last_day": last_day.isoformat() if last_day else None,
        "rows_year": rows_year,
        "day_of_year": day_of_year,
        "days_in_year": days_in_year,
        "pace": rows_year - day_of_year,
        "today_count": counts.get(today, 0),
        "status": status,
    }


def read_snapshot(path):
    """Read + parse the log file. Raises OSError when unreadable."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    counts, last_ts, total = parse_log(text)
    return summarize(counts, last_ts, total)


# ---- drawing --------------------------------------------------------------

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


def _draw(screen, title, bg, snap, path, health, error, updated):
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

    foot = path or "no log configured"
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


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or "ROWING").upper()
    bg = screen.color(params.get("background"), C_BG)
    try:
        interval = int(params.get("interval") or POLL_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = POLL_DEFAULT_INTERVAL
    interval = max(5, min(3600, interval))
    path = resolve_path(params.get("path"))

    if path is None:
        _draw_message(screen, title, bg, "NO LOG CONFIGURED",
                      "set path param or $%s" % ENV_VAR, None, C_WARN)
        while not stop.is_set():
            stop.wait(interval)
            now_path = resolve_path(params.get("path"))
            if now_path is not None:
                path = now_path
                break
        else:
            return

    snap = None
    health = "cold"
    error = None
    updated = 0.0
    try:
        snap = read_snapshot(path)
        updated = time.time()
        health = "warm"
    except Exception as err:
        error = str(err)[:160]
        health = "error"
        _draw_message(screen, title, bg, "LOG NOT READABLE",
                      path, ("last error: %s" % error) if error else None,
                      C_FAILED)

    if snap is not None:
        _draw(screen, title, bg, snap, path, health, error, updated)
    last_key = _snapshot_key(snap, health)
    last_draw = time.time()
    while not stop.is_set():
        stop.wait(interval)
        if stop.is_set():
            break
        try:
            fresh = read_snapshot(path)
            snap = fresh
            updated = time.time()
            health = "warm"
            error = None
        except Exception as err:
            error = str(err)[:160]
            health = "stale" if snap is not None else "error"
        key = _snapshot_key(snap, health)
        now = time.time()
        if key != last_key or (now - last_draw) >= DRAW_REFRESH:
            last_key = key
            last_draw = now
            try:
                if snap is None:
                    _draw_message(screen, title, bg, "LOG NOT READABLE",
                                  path, ("last error: %s" % error) if error else None,
                                  C_FAILED)
                else:
                    _draw(screen, title, bg, snap, path, health, error, updated)
            except Exception:
                # A draw failure must not kill the daemon loop; the last
                # good frame stays on the panel.
                pass
