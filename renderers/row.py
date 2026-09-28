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

import time

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

from row_draw import (BIG_SIZE, C_BG, C_DIM, C_FAILED, C_FIRE, C_LINE,
                    C_TEXT, C_UP, C_WARN, FOOT_SIZE, HEADER_SIZE, LABEL_SIZE,
                    PAD, ROW_SIZE, SUB_SIZE, _age, _draw, _draw_message, _fit,
                    _font, _font_or_default, _snapshot_key)

POLL_DEFAULT_INTERVAL = 60
DRAW_REFRESH = 60  # re-render at least this often so the age line stays honest

from row_source import (DEFAULT_SOURCE, ENV_JOURNAL, ENV_SOURCE, ENV_VAR,
                        FETCH_TIMEOUT_DEFAULT, FETCH_TIMEOUT_MAX,
                        FETCH_TIMEOUT_MIN, classify_source, parse_timeout,
                        resolve_journal, resolve_path, resolve_source,
                        source_label)

from row_streak import _step, compute_streaks, parse_log, summarize


from row_poll import (HTTP_MAX_BYTES, MIN_ROW_DISTANCE_M, fetch_http_text,
                    load_journal, merge_journal, note_sighting, poll_source,
                    read_snapshot, save_journal, stats_sighting)

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

