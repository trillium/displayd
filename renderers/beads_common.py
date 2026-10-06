"""Shared vocabulary for the beads views: the look and the text fitting.

Single concept: what every beads frame agrees on before it starts drawing --
the palette, the four-bucket presentation table (glyph/label/colour, the
same keys beads_buckets classifies into), the page pad, the poll/draw
cadence defaults, and the pixel text-fitting helpers the tags, rows, and
cards all measure with. NOT a renderer: no run(), so the daemon's loader
skips this file. Bucket classification lives in beads_buckets.py, fetching
in beads_source.py, and the poll cache in beads_poll.py.
"""

from PIL import ImageFont

# Poll interval when the view is given none, and how often a frame is
# re-rendered even when nothing changed (so the age line stays honest).
POLL_FALLBACK_INTERVAL = 60
DRAW_REFRESH = 15


C_BG = (8, 8, 12)
C_ROLLING = (80, 220, 120)
C_LINEDUP = (110, 180, 255)
C_STALLED = (255, 180, 60)
C_PAST = (130, 130, 140)
C_TEXT = (235, 235, 240)
C_DIM = (140, 140, 150)
C_LINE = (60, 60, 70)
C_ERR = (255, 90, 90)

BUCKETS = (
    ("rolling", "\u25cf", "Rolling", C_ROLLING, "in progress"),
    ("linedup", "\u266a", "Lined Up", C_LINEDUP, "open, nothing in the way"),
    ("stalled", "\u2298", "Stalled", C_STALLED, "waiting on something"),
    ("past", "\u2713", "Past the Stand", C_PAST, "closed"),
)
BUCKET_BY_KEY = {key: (glyph, label, color) for key, glyph, label, color, _ in BUCKETS}

PAD = 60


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


def _fit(draw, text, font, max_w, max_chars=90):
    """Shrink text to fit max_w pixels wide (and max_chars characters).

    Guarantee: with a working font the return value is never wider than
    max_w. Text that fits is returned unchanged; longer text is shaved
    until it fits. When even a single glyph overflows max_w, the result
    is an ellipsis if it fits, else an empty string -- a cut is always
    signalled without overflowing. Callers (e.g. the beads attention
    rows, which fit the title into the width left over by the reason)
    rely on this: a fitted string always honours the budget it was
    given, so measuring a fitted string gives a truthful remainder.

    No-font fallback is deliberate: with font None there are no pixel
    metrics, so _fit keeps the character budget only (text[:max_chars])
    and ignores max_w. A missing font must truncate, never blank a row.
    """
    text = str(text or "")
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars]
    if font is None:
        return text  # intentional: no metrics, character budget only
    try:
        if max_w <= 0:
            return ""
        while draw.textlength(text, font=font) > max_w and len(text) > 1:
            text = text[:-2] if len(text) > 8 else text[:-1]
        if draw.textlength(text, font=font) <= max_w:
            return text
        # Even one glyph overflows: ellipsis if it fits, else empty.
        try:
            return ("\u2026" if draw.textlength("\u2026", font=font)
                    <= max_w else "")
        except Exception:
            return ""
    except Exception:
        return text[:max_chars] if len(text) > max_chars else text


def _wrap(draw, text, font, max_w, max_rows=3):
    """Greedy word-wrap to pixel width; returns at most max_rows rows.

    When words are dropped to respect max_rows, the final row keeps a
    trailing ellipsis marker: room for the marker is reserved before
    fitting, so the fit can never shave the marker itself back off.
    Rows that fit without dropping anything carry no marker. A single
    word wider than max_w stays whole on its own row (it cannot wrap),
    so _wrap always terminates with a non-empty result.

    Guarantee: with a working font no returned row is wider than max_w.
    Whole words that already fit pass through untouched; only a row that
    still overflows (a single word wider than the budget) is rescued via
    _fit, which may cut it mid-word rather than let it run off-panel."""
    words = str(text or "").split()
    if not words:
        return [""]
    rows, cur = [], ""
    truncated = False
    for word in words:
        trial = (cur + " " + word).strip()
        try:
            too_wide = font is not None and draw.textlength(trial, font=font) > max_w
        except Exception:
            too_wide = len(trial) > 90
        if too_wide and cur:
            rows.append(cur)
            cur = word
            if len(rows) >= max_rows:
                truncated = True
                break
        else:
            cur = trial
    else:
        rows.append(cur)
    rows = rows[:max_rows]
    if font is not None:
        # Rescue-fit only: _fit returns fitting text unchanged, so whole
        # words pass through byte-identical and only an overflowing row
        # (a single word wider than the budget) gets cut down to size.
        # max_chars=len(row) keeps the character budget out of the way:
        # this pass is purely about pixel width.
        for i in range(len(rows)):
            if truncated and i == len(rows) - 1:
                continue  # marker fit below owns the final row
            rows[i] = _fit(draw, rows[i], font, max_w, len(rows[i]))
    if truncated and rows:
        rows[-1] = _fit_with_marker(draw, rows[-1], font, max_w)
    return rows or [""]


def _fit_with_marker(draw, text, font, max_w, marker=" \u2026"):
    """Fit text reserving room for a trailing marker, then append it.

    Used for the final wrapped row: unlike _fit(text + marker), which
    would shave the marker itself back off a full row, this guarantees
    the marker survives whenever it fits at all."""
    try:
        marker_w = (draw.textlength(marker, font=font)
                    if font is not None else len(marker))
    except Exception:
        marker_w = len(marker)
    if font is None:
        # No metrics: character budget only, marker always survives.
        return (text + marker)[:len(text) + len(marker)]
    if marker_w > max_w:
        return _fit(draw, "\u2026", font, max_w)
    budget = len(text) + len(marker)
    return _fit(draw, text, font, max_w - marker_w, budget) + marker
