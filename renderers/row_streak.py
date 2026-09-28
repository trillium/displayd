"""Streak math for the row renderer (pure functions, no I/O).

Single-concept split of renderers/row.py: log parsing plus the
rest-day bank rule (`_step`, `compute_streaks`, `summarize`). Mirrors
`row.sh` exactly. Only stdlib `datetime` is needed.
"""

import datetime


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
