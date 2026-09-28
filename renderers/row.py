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

Remote source: by default the streak is sourced live from the mini1 PM5
bridge WebSocket that also feeds OBS (`ws://mini1:8765/obs/ws`) -- the
same feed, never a second serving path. A poll that observes a real
workout underway (rower connected, distance past warm-up) journals the
calendar day to a small local journal file; streaks are computed over
the union of the remote journal and the local rows.txt fallback, so
history survives and a dead remote degrades to a stale-marked
last-known streak instead of a blank frame.
"""

import datetime
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request

from PIL import ImageDraw, ImageFont

from row_ws import (WS_MAX_BYTES, WS_MAX_MESSAGES, fetch_ws_stats)

NAME = "row"
DESCRIPTION = "Concept2 rowing streak: current day/row streak, last row, year pace (from row_tracker rows.txt)"
STATIC = False
# Playlist progress-bar colour for this view (see playlist.accent_for).
ACCENT = "#5CFF9D"
PARAMS = {
    "path": {"type": "string", "help": "local rows.txt fallback; default $DISPLAYD_ROW_FILE, else sibling row_tracker checkout"},
    "source": {"type": "string", "help": "primary row source: ws(s) URL of the mini1 PM5 stats feed, http(s) URL of rows.txt text, or a file path; default $DISPLAYD_ROW_SOURCE, else ws://mini1:8765/obs/ws"},
    "timeout": {"type": "integer", "help": "remote fetch seconds, default 10 (clamped 2..60)"},
    "journal": {"type": "string", "help": "sightings journal path; default $DISPLAYD_ROW_JOURNAL, else next to the local log"},
    "title": {"type": "string", "help": "header text, default ROWING"},
    "interval": {"type": "integer", "help": "poll seconds, default 60"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}

# A remote sighting counts as a row only past this distance: filters the
# paired-but-idle PM5 (0 m) while catching any real workout within a poll.
MIN_ROW_DISTANCE_M = 100.0
HTTP_MAX_BYTES = 512 * 1024
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


from row_source import (DEFAULT_SOURCE, ENV_JOURNAL, ENV_SOURCE, ENV_VAR,
                        FETCH_TIMEOUT_DEFAULT, FETCH_TIMEOUT_MAX,
                        FETCH_TIMEOUT_MIN, classify_source, parse_timeout,
                        resolve_journal, resolve_path, resolve_source,
                        source_label)

from row_streak import _step, compute_streaks, parse_log, summarize


def read_snapshot(path):
    """Read + parse the log file. Raises OSError when unreadable."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    counts, last_ts, total = parse_log(text)
    return summarize(counts, last_ts, total)


# ---- remote fetching (stdlib only; never blocks the panel) ---------------

def fetch_http_text(url, timeout):
    """GET rows.txt text from an http(s) URL. Raises OSError/ValueError."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("row text url must be http(s)")
    req = urllib.request.Request(url, headers={"User-Agent": "displayd-row/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if getattr(resp, "status", 200) != 200:
            raise OSError("http %s" % getattr(resp, "status", "?"))
        raw = resp.read(HTTP_MAX_BYTES + 1)
    if len(raw) > HTTP_MAX_BYTES:
        raise ValueError("row text too large")
    return raw.decode("utf-8", errors="replace")


# ---- sightings journal (remote days, without bank inflation) --------------

def stats_sighting(msg, last_sample=None):
    """Decide whether one stats message evidences a row today.
    Returns (sighted, sample). Requires a real workout underway
    (rower connected, distance past warm-up, elapsed ticking) and a
    sample newer than the persisted one, so a frozen feed cannot
    re-journal day after day."""
    if not isinstance(msg, dict):
        return False, None
    if msg.get("connected") is False:
        return False, None
    raw = msg.get("raw")
    if not isinstance(raw, dict):
        return False, None
    try:
        dist = float(raw.get("distance_m"))
        elapsed = float(raw.get("elapsed_time_s"))
    except (TypeError, ValueError):
        return False, None
    if not (dist >= MIN_ROW_DISTANCE_M and elapsed > 0):
        return False, None
    sample = {"distance_m": dist, "elapsed_time_s": elapsed}
    if isinstance(last_sample, dict):
        try:
            if (float(last_sample.get("distance_m")) == dist
                    and float(last_sample.get("elapsed_time_s")) == elapsed):
                return False, sample
        except (TypeError, ValueError):
            pass
    return True, sample


def load_journal(path):
    """(days: set of ISO dates, last_sample: dict|None). A missing or
    corrupt journal reads as empty -- never raises."""
    days = set()
    last = None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return days, last
    if isinstance(data, dict):
        raw_days = data.get("days")
        if isinstance(raw_days, list):
            for entry in raw_days:
                if isinstance(entry, str) and len(entry) == 10:
                    try:
                        datetime.date.fromisoformat(entry)
                        days.add(entry)
                    except ValueError:
                        continue
        if isinstance(data.get("last_sample"), dict):
            last = data["last_sample"]
    return days, last


def save_journal(path, days, last_sample):
    """Atomic journal write (tmp + replace). Raises OSError on failure."""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"days": sorted(days), "last_sample": last_sample}, fh)
        fh.write("\n")
    os.replace(tmp, path)


def note_sighting(path, day_iso, sample):
    """Journal one sighting. Returns True when the day is newly added.
    Never raises: a broken journal must not break the panel."""
    try:
        days, _last = load_journal(path)
        fresh = day_iso not in days
        days.add(day_iso)
        save_journal(path, days, sample)
        return fresh
    except Exception:
        return False


def merge_journal(counts, last_ts, total, journal_days):
    """Fold journal ISO days into parsed-log data. Journal days count one
    row each and only when the log has no entry that day, so the bank is
    never inflated; the newest journal day refreshes last_ts when newer."""
    counts = dict(counts or {})
    for iso in sorted(journal_days or ()):
        try:
            day = datetime.date.fromisoformat(iso)
        except ValueError:
            continue
        if day not in counts:
            counts[day] = 1
            total += 1
            stamp = iso + "T12:00:00"
            if last_ts is None or stamp > last_ts:
                last_ts = stamp
    return counts, last_ts, total


def poll_source(kind, target, timeout, local_path, journal_path, today=None):
    """One poll across remote source + local fallback + journal. Returns
    (counts, last_ts, total, remote_ok, remote_error, sighted). Never
    raises: every failure surfaces as remote_error with whatever the
    fallback and journal still provide."""
    remote_ok = True
    remote_error = None
    sighted = False
    remote_counts, remote_last, remote_total = {}, None, 0
    day = today or datetime.date.today()
    try:
        if kind == "file":
            if not target:
                raise OSError("no log configured")
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            remote_counts, remote_last, remote_total = parse_log(text)
        elif kind == "text-url":
            text = fetch_http_text(target, timeout)
            remote_counts, remote_last, remote_total = parse_log(text)
        else:  # live stats feed: sightings journal the day
            msg = fetch_ws_stats(target, timeout)
            # msg None = reachable but quiet (PM5 idle): healthy, no sighting.
            _days, last = load_journal(journal_path)
            ok, sample = stats_sighting(msg, last)
            if sample is not None:
                try:
                    days, _last = load_journal(journal_path)
                    days.add(day.isoformat())
                    save_journal(journal_path, days, sample)
                except Exception:
                    pass
            sighted = ok
    except Exception as err:
        remote_ok = False
        remote_error = str(err)[:160]
    counts, last_ts, total = dict(remote_counts), remote_last, remote_total
    if local_path and (kind != "file" or local_path != target):
        try:
            with open(local_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            lcounts, llast, _ltotal = parse_log(text)
            for d, c in lcounts.items():
                if d not in counts:
                    counts[d] = c
                    total += c
        except Exception:
            pass
        else:
            if llast is not None and (last_ts is None or llast > last_ts):
                last_ts = llast
    try:
        jdays, _last = load_journal(journal_path)
    except Exception:
        jdays = set()
    counts, last_ts, total = merge_journal(counts, last_ts, total, jdays)
    return counts, last_ts, total, remote_ok, remote_error, sighted


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


def run(screen, params, stop):
    params = params or {}
    title = str(params.get("title") or "ROWING").upper()
    bg = screen.color(params.get("background"), C_BG)
    try:
        interval = int(params.get("interval") or POLL_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = POLL_DEFAULT_INTERVAL
    interval = max(5, min(3600, interval))
    timeout = parse_timeout(params.get("timeout"))
    source = resolve_source(params.get("source"))
    kind, target = classify_source(source)
    path = resolve_path(params.get("path"))
    journal_path = resolve_journal(params.get("journal"), path)
    label = source_label(kind, target, path)

    if kind == "file" and not target and path is None:
        _draw_message(screen, title, bg, "NO LOG CONFIGURED",
                      "set source param or $%s" % ENV_SOURCE, None, C_WARN)
        while not stop.is_set():
            stop.wait(interval)
            now_source = resolve_source(params.get("source"))
            now_kind, now_target = classify_source(now_source)
            now_path = resolve_path(params.get("path"))
            if (now_kind != "file" or now_target) or now_path is not None:
                source, kind, target, path = now_source, now_kind, now_target, now_path
                journal_path = resolve_journal(params.get("journal"), path)
                label = source_label(kind, target, path)
                break
        else:
            return

    def _refresh():
        """One poll shaped into (snap|None, health, error)."""
        counts, last_ts, total, ok, err, _sighted = poll_source(
            kind, target, timeout, path, journal_path)
        if counts or last_ts is not None:
            return summarize(counts, last_ts, total), \
                ("warm" if ok else "stale"), err
        if ok:
            # Reachable source, genuinely no rows anywhere yet.
            return summarize(counts, last_ts, total), "warm", err
        return None, "error", err or "no row data"

    snap = None
    health = "cold"
    error = None
    updated = 0.0
    try:
        snap, health, error = _refresh()
        if snap is not None:
            updated = time.time()
        else:
            raise OSError(error or "no row data")
    except Exception as err:
        error = str(err)[:160]
        health = "error"
        _draw_message(screen, title, bg, "LOG NOT READABLE",
                      label, ("last error: %s" % error) if error else None,
                      C_FAILED)

    if snap is not None:
        _draw(screen, title, bg, snap, label, health, error, updated)
    last_key = _snapshot_key(snap, health)
    last_draw = time.time()
    while not stop.is_set():
        stop.wait(interval)
        if stop.is_set():
            break
        try:
            fresh, fresh_health, fresh_error = _refresh()
            if fresh is not None:
                snap = fresh
                updated = time.time()
                error = fresh_error
                health = fresh_health
            else:
                error = fresh_error
                health = "stale" if snap is not None else "error"
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
                                  label, ("last error: %s" % error) if error else None,
                                  C_FAILED)
                else:
                    _draw(screen, title, bg, snap, label, health, error, updated)
            except Exception:
                # A draw failure must not kill the daemon loop; the last
                # good frame stays on the panel.
                pass
