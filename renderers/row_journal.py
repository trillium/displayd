"""Sightings journal for the row renderer: remote days, no bank inflation.

Single concept: everything about the small local journal file that records
calendar days on which the mini1 PM5 feed showed a real workout underway --
deciding whether one stats message evidences a row, atomic journal reads
and writes, and folding journal days into parsed-log data one row each so
the bank is never inflated. A missing or corrupt journal reads as empty and
a broken write reports False: the journal must never break the panel.
Pure journal logic only; fetching and drawing live in row.py.
"""

import datetime
import json
import os

# A remote sighting counts as a row only past this distance: filters the
# paired-but-idle PM5 (0 m) while catching any real workout within a poll.
MIN_ROW_DISTANCE_M = 100.0


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
