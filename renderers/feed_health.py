"""Operational dashboard for the panel itself: health of every active feed.

Reads the daemon's FeedStore snapshot (no inputs of its own) and draws one
row per registered (renderer, input) feed: name, bound renderer, health
state, and time since the last update. Redraws every few seconds so ages
and health labels stay current while selected.

Presentation only, and every pixel comes from the component layer: the
band is ``ui.shell`` (title, overall status line, rule) and one feed is
``ui.stat.list_row`` (status dot, name, right-aligned value). The four
health words wear ``shell.health_ink``'s palette roles and an age is
``shell.short_age``, so this module owns which feed goes where and nothing
else -- it holds no colour map, no font loader and no age arithmetic of
its own, because those are exactly the things it used to duplicate.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import theme
from ui import shell, stat

NAME = "feed_health"
DESCRIPTION = "Dashboard of feed health (POST /show {\"renderer\": \"feed_health\"})"
STATIC = False
# A band and a list of rows: it reads correctly in a split half too, so a
# preset may put it in one (capability.py; enforced by POST /layout).
CAPABILITY = "partial"
PARAMS = {
    "title": {"type": "string", "help": "header text, default FEED HEALTH"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}
INPUTS = {}

POLL = 5.0    # seconds between redraws; ages stay current without manual refresh
ROW_Y = 190   # the first feed's row, under the band's rule
ROW_H = 92    # one feed's height on the panel
FLOOR = 60    # room kept at the bottom for the "+N more" line


def collect_rows(snapshot):
    """Flatten a FeedStore snapshot into sorted row dicts the drawer eats."""
    rows = []
    for renderer in sorted(snapshot):
        for input_name in sorted(snapshot[renderer]):
            meta = snapshot[renderer][input_name] or {}
            health = meta.get("health") or "cold"
            rows.append({
                "renderer": renderer,
                "input": input_name,
                "name": "%s.%s" % (renderer, input_name),
                "health": health,
                "ink": shell.health_ink(health),
                "age": shell.short_age(meta.get("age_seconds")),
                "count": meta.get("count", 0),
            })
    return rows


def summarize(rows):
    """Overall system health line: ALL HEALTHY / DEGRADED / NO FEEDS."""
    if not rows:
        return "NO FEEDS", shell.health_ink("cold")
    counts = {}
    for row in rows:
        counts[row["health"]] = counts.get(row["health"], 0) + 1
    parts = ["%d %s" % (counts[h], h)
             for h in ("warm", "stale", "error", "cold") if counts.get(h)]
    if set(counts) <= {"warm"}:
        return "ALL HEALTHY -- " + "  ".join(parts), shell.health_ink("warm")
    worst = "error" if counts.get("error") else "stale" if counts.get("stale") else "cold"
    return "DEGRADED -- " + "  ".join(parts), shell.health_ink(worst)


def _draw(screen, title, rows, summary, summary_ink, bg):
    img = screen.new_image(bg)
    shell.head(img, screen, title, status=summary, status_ink=summary_ink)
    col_w = screen.W - 2 * shell.PAD

    if not rows:
        # An empty dashboard says it is waiting rather than looking broken.
        stat.body(img, screen, (shell.PAD, ROW_Y),
                  "no feeds registered \u2014 waiting for one to be pushed",
                  size=stat.LABEL_SIZE, room=col_w)
        return img

    y = ROW_Y
    shown = 0
    for row in rows:
        if y + ROW_H > screen.H - FLOOR:
            break
        stat.list_row(img, screen, (shell.PAD, y), row["name"],
                      "%s  %s ago  x%d" % (row["health"].upper(), row["age"],
                                           row["count"]),
                      ink=theme.rgb("ink"), meta_ink=row["ink"],
                      dot_ink=row["ink"])
        y += ROW_H
        shown += 1
    if shown < len(rows):
        stat.body(img, screen, (shell.PAD, screen.H - FLOOR + 10),
                  "+%d more" % (len(rows) - shown), size=stat.LINE_SIZE,
                  room=col_w)
    return img


def run(screen, params, stop):
    title = str(params.get("title") or "FEED HEALTH")
    bg = screen.color(params.get("background"), theme.rgb("page"))
    while not stop.is_set():
        try:
            snapshot = screen.feeds.snapshot() if screen.feeds is not None else {}
        except Exception:
            snapshot = {}
        rows = collect_rows(snapshot)
        summary, summary_ink = summarize(rows)
        # One complete frame, one swap: never a partial dashboard.
        screen.present(_draw(screen, title, rows, summary, summary_ink, bg))
        stop.wait(POLL)
