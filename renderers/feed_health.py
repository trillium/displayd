"""Operational dashboard for the panel itself: health of every active feed.

Reads the daemon's FeedStore snapshot (no inputs of its own) and draws one
row per registered (renderer, input) feed: name, bound renderer, health
state, and time since the last update. Redraws every few seconds so ages
and health labels stay current while selected.

Health colours: green=warm, yellow=stale, red=error, grey=cold.
"""

import time

from PIL import ImageDraw, ImageFont

NAME = "feed_health"
DESCRIPTION = "Dashboard of feed health (POST /show {\"renderer\": \"feed_health\"})"
STATIC = False
PARAMS = {
    "title": {"type": "string", "help": "header text, default FEED HEALTH"},
    "background": {"type": "string", "help": "background colour, default near-black"},
}
INPUTS = {}

POLL = 5.0  # seconds between redraws; ages stay current without manual refresh
HEADER_H = 150
PAD = 48
ROW_H = 92
NAME_SIZE = 44
META_SIZE = 38
HEAD_SIZE = 54

HEALTH_COLORS = {
    "warm": (80, 220, 120),    # green
    "stale": (240, 200, 60),   # yellow
    "error": (255, 80, 80),    # red
    "cold": (128, 128, 128),   # grey
}


def _font(screen, name, size):
    path = screen.font_path(name)
    if path is None:
        return None
    return ImageFont.truetype(path, size)


def format_age(age_seconds):
    """Short human age for a feed: '12s', '3m', '2h', '9d', or 'never'."""
    if age_seconds is None:
        return "never"
    try:
        age = max(0.0, float(age_seconds))
    except (TypeError, ValueError):
        return "never"
    if age < 60:
        return "%ds" % int(age)
    if age < 3600:
        return "%dm" % int(age // 60)
    if age < 86400:
        return "%dh" % int(age // 3600)
    return "%dd" % int(age // 86400)


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
                "color": HEALTH_COLORS.get(health, HEALTH_COLORS["cold"]),
                "age": format_age(meta.get("age_seconds")),
                "count": meta.get("count", 0),
            })
    return rows


def summarize(rows):
    """Overall system health line: ALL HEALTHY / DEGRADED / NO FEEDS."""
    if not rows:
        return "NO FEEDS", HEALTH_COLORS["cold"]
    counts = {}
    for row in rows:
        counts[row["health"]] = counts.get(row["health"], 0) + 1
    parts = ["%d %s" % (counts[h], h)
             for h in ("warm", "stale", "error", "cold") if counts.get(h)]
    if set(counts) <= {"warm"}:
        return "ALL HEALTHY -- " + "  ".join(parts), HEALTH_COLORS["warm"]
    worst = "error" if counts.get("error") else "stale" if counts.get("stale") else "cold"
    return "DEGRADED -- " + "  ".join(parts), HEALTH_COLORS[worst]


def _draw(screen, title, rows, summary, summary_color, bg):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font(screen, "DejaVuSans-Bold", HEAD_SIZE)
    name_font = _font(screen, "DejaVuSans-Bold", NAME_SIZE)
    meta_font = _font(screen, "DejaVuSans", META_SIZE)

    draw.text((PAD, 26), title, font=head_font or meta_font, fill=(255, 255, 255))
    draw.text((PAD, 26 + HEAD_SIZE + 8), summary,
              font=meta_font, fill=summary_color)
    draw.line([(PAD, HEADER_H - 14), (screen.W - PAD, HEADER_H - 14)],
              fill=(60, 60, 70), width=2)

    if not rows:
        draw.text((PAD, HEADER_H + 40), "no feeds registered",
                  font=meta_font, fill=(120, 120, 130))
        return img

    y = HEADER_H + 10
    shown = 0
    for row in rows:
        if y + ROW_H > screen.H - 40:
            break
        # Status dot.
        draw.ellipse([(PAD, y + 22), (PAD + 36, y + 58)], fill=row["color"])
        draw.text((PAD + 56, y), row["name"],
                  font=name_font or meta_font, fill=(235, 235, 240))
        right = "%s  %s ago  x%d" % (row["health"].upper(), row["age"], row["count"])
        if meta_font is not None:
            draw.text((screen.W - PAD - draw.textlength(right, font=meta_font), y + 8),
                      right, font=meta_font, fill=row["color"])
        else:
            draw.text((PAD + 56, y + NAME_SIZE + 4), right, fill=row["color"])
        y += ROW_H
        shown += 1
    if shown < len(rows):
        draw.text((PAD, screen.H - 70), "+%d more" % (len(rows) - shown),
                  font=meta_font, fill=(120, 120, 130))
    return img


def run(screen, params, stop):
    title = str(params.get("title") or "FEED HEALTH")
    bg = screen.color(params.get("background"), (10, 10, 14))
    while not stop.is_set():
        try:
            snapshot = screen.feeds.snapshot() if screen.feeds is not None else {}
        except Exception:
            snapshot = {}
        rows = collect_rows(snapshot)
        summary, summary_color = summarize(rows)
        # One complete frame, one swap: never a partial dashboard.
        screen.present(_draw(screen, title, rows, summary, summary_color, bg))
        stop.wait(POLL)
