"""Painters for the single-bead detail card: full card, empty card.

beads_detail.py (target selection, poll loop, empty-state rules) owns the
view lifecycle. Everything that puts pixels up -- the full detail card
with its WHY/HOLDS UP/LATEST sections and the honest empty card -- lives
here so the view stays within the project's line budget.
"""

import os
import sys

from PIL import ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from beads_age import _age, age_of
from ui import shell as ui_shell
from beads_common import (
    BUCKET_BY_KEY,
    C_DIM,
    C_LINE,
    C_STALLED,
    C_TEXT,
    _fit,
    _font,
    _wrap,
)

PAD = 60
TITLE_SIZE = 64
HEAD_SIZE = 44
META_SIZE = 34
ROW_SIZE = 38
SMALL_SIZE = 30


def _state_line(issue):
    bits = ["P%d" % issue["priority"], issue["issue_type"]]
    who = issue.get("assignee") or issue.get("owner")
    if who:
        bits.append("owner %s" % who)
    if issue.get("created_at"):
        bits.append("age %s" % age_of(issue["created_at"]))
    if issue.get("updated_at"):
        bits.append("touched %s ago" % age_of(issue["updated_at"]))
    return " \u00b7 ".join(bits)


def _draw_card(screen, issue, bucket, waiters, dependents, snap,
               health, updated, source, bg):
    glyph, label, color = BUCKET_BY_KEY[bucket]
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    head_font = _font(screen, "DejaVuSans-Bold", TITLE_SIZE)
    sec_font = _font(screen, "DejaVuSans-Bold", HEAD_SIZE)
    meta_font = _font(screen, "DejaVuSans", META_SIZE)
    row_font = _font(screen, "DejaVuSans", ROW_SIZE)
    small_font = _font(screen, "DejaVuSans", SMALL_SIZE)
    plain = head_font or row_font
    W = screen.W - 2 * PAD
    bottom = screen.H - 70

    y = 24
    # Header: identity left, bucket pill + health right.
    ident = "[%s %s]" % (issue["store"], issue["id"])
    draw.text((ui_shell.band_pad(screen), y + 8), ident, font=meta_font or plain, fill=C_DIM)
    pill = "%s %s" % (glyph, label.upper())
    pill_w = 0
    if sec_font is not None:
        try:
            pill_w = draw.textlength(pill, font=sec_font)
        except Exception:
            pass
    draw.text((screen.W - PAD - pill_w, y), pill, font=sec_font or plain,
              fill=color)
    status = "%s \u00b7 %s" % (health, _age(updated))
    draw.text((screen.W - PAD - pill_w, y + 56),
              _fit(draw, status, small_font, pill_w, 40),
              font=small_font or plain, fill=C_DIM)
    y += 118
    draw.line([(PAD, y), (screen.W - PAD, y)], fill=C_LINE, width=2)
    y += 22

    # Title, up to three wall-sized rows.
    for row in _wrap(draw, issue["title"], head_font, W, 3):
        if y > bottom - 200:
            break
        draw.text((PAD, y), row, font=plain, fill=C_TEXT)
        y += TITLE_SIZE + 12
    y += 6
    draw.text((PAD, y), _fit(draw, _state_line(issue), meta_font, W),
              font=meta_font or plain, fill=C_DIM)
    y += META_SIZE + 22

    # About: first breath of the description for context, never the whole.
    about = (issue.get("description") or "").strip().splitlines()
    about = " ".join(line.strip() for line in about if line.strip())
    if about and y <= bottom - 200:
        for row in _wrap(draw, about, meta_font, W, 2):
            if y > bottom - 200:
                break
            draw.text((PAD, y), row, font=meta_font or plain, fill=C_DIM)
            y += META_SIZE + 10
        y += 10

    def section(head, color=C_TEXT):
        nonlocal_y = [y]

        def emit(line, font, fill, indent=0):
            yy = nonlocal_y[0]
            if yy > bottom - 40:
                return False
            draw.text((PAD + indent, yy),
                      _fit(draw, line, font, W - indent),
                      font=font or plain, fill=fill)
            nonlocal_y[0] = yy + ROW_SIZE + 10
            return True

        if nonlocal_y[0] > bottom - 120:
            return None
        draw.text((PAD, nonlocal_y[0]), head, font=sec_font or plain,
                  fill=color)
        nonlocal_y[0] += HEAD_SIZE + 12
        return emit

    # Why this bucket: the derivation, not a status read.
    emit = section("WHY " + label.upper(), C_STALLED)
    if emit is not None:
        if bucket == "stalled":
            for w in waiters[:3]:
                if not emit("waits on %s \u2014 %s (%s)" % (
                        w["id"], w["title"], w["status"].upper()),
                        row_font, C_TEXT, indent=20):
                    break
        elif bucket == "past":
            closed = issue.get("closed_at")
            note = "closed"
            if closed:
                note += " " + str(closed)[:10]
            if issue.get("close_reason"):
                note += " \u2014 %s" % issue["close_reason"]
            emit(note, row_font, C_TEXT, indent=20)
        else:
            emit("nothing unfinished in the way", row_font, C_TEXT, indent=20)

    # What it holds up.
    live_deps = [d for d in dependents if d["status"] != "closed"]
    if live_deps:
        emit = section("HOLDS UP %d" % len(live_deps))
        if emit is not None:
            for d in live_deps[:2]:
                if not emit("%s \u2014 %s (%s)" % (
                        d["id"], d["title"], d["status"].upper()),
                        row_font, C_TEXT, indent=20):
                    break

    # Latest notes: the freshest few earn wall space, history does not.
    comments = issue.get("comments") or []
    if comments:
        emit = section("LATEST")
        if emit is not None:
            for c in comments[-3:]:
                line = "%s: %s" % (c.get("author") or "?",
                                   (c.get("text") or "").splitlines()[0]
                                   if (c.get("text") or "").splitlines() else "")
                if c.get("created_at"):
                    line += "  [%s ago]" % age_of(c["created_at"])
                if not emit(line, row_font, C_TEXT, indent=20):
                    break

    # Footer: provenance + the way back to the overview.
    foot = "back: POST /show {\"renderer\":\"beads\"}"
    if source:
        foot += "   (%s)" % source
    draw.text((PAD, screen.H - 56), _fit(draw, foot, small_font, W),
              font=small_font or plain, fill=C_DIM)
    screen.present(img)


def _draw_empty(screen, msg, hint, error, bg):
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    row_font = _font(screen, "DejaVuSans", ROW_SIZE)
    small_font = _font(screen, "DejaVuSans", SMALL_SIZE)
    plain = row_font
    draw.text((PAD, 220), _fit(draw, msg, row_font, screen.W - 2 * PAD),
              font=plain, fill=C_DIM)
    draw.text((PAD, 300), _fit(draw, hint, small_font, screen.W - 2 * PAD),
              font=small_font or plain, fill=C_DIM)
    if error:
        draw.text((PAD, 360),
                  _fit(draw, "last error: " + error, small_font,
                       screen.W - 2 * PAD),
                  font=small_font or plain, fill=(255, 90, 90))
    screen.present(img)
