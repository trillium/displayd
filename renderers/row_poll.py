"""Polling and fetching for the rowing-streak view. Single concept: poll.

Log-file snapshot reads, http(s) rows.txt fetching, and the one poll
across remote source + local fallback + sightings journal. Never raises
out of poll_source: failures surface as remote_error. Imported and
re-exported by row.py so existing references (row_view.poll_source,
row_view.read_snapshot, ...) keep working.
"""

import datetime
import urllib.request

from row_journal import (MIN_ROW_DISTANCE_M, load_journal, merge_journal,
                         note_sighting, save_journal, stats_sighting)
from row_streak import parse_log, summarize
from row_ws import fetch_ws_stats

HTTP_MAX_BYTES = 512 * 1024


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
