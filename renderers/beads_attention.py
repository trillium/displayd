"""Below-the-buckets half of the beads parade: tally, attention, footer.

beads.py (the parade overview) owns the header, the cold-start frame, and
the four-bucket strip. Everything from the progress tally down -- the
"N closed of M" bar, the NEEDS THE CAPTAIN rows, and the per-store footer
-- lives here so the overview stays within the project's line budget.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import beads_style as style_mod
from beads_common import (
    C_DIM,
    C_LINE,
    C_ROLLING,
    C_STALLED,
    C_TEXT,
    _fit,
)

# Icon glyphs render at the same size as the attention rows in beads.py
# (its ROW_SIZE); kept in sync by hand, one integer, no import cycle.
ROW_SIZE = 40


def _text_w(draw, text, font):
    """Pixel width of text, 0 when unmeasurable -- never raises."""
    if font is None:
        return 0
    try:
        return draw.textlength(text, font=font)
    except Exception:
        return 0


def draw_lower(draw, screen, snap, cfg, *, source, health, error, pad,
               lab_font, row_font, sub_font, small_font, plain):
    """Draw tally bar, NEEDS THE CAPTAIN rows, and footer in place."""
    # Running tally + progress.
    total = snap["total"] or 1
    frac = snap["closed"] / total
    bar_y, bar_h = 420, 26
    draw.rectangle([pad, bar_y, screen.W - pad, bar_y + bar_h],
                   outline=C_LINE, width=2)
    fill_w = (screen.W - 2 * pad - 8) * frac
    if fill_w > 2:
        draw.rectangle([pad + 4, bar_y + 4, pad + 4 + fill_w,
                        bar_y + bar_h - 4], fill=C_ROLLING)
    tally = "%d closed of %d \u00b7 %d%% past the stand" % (
        snap["closed"], snap["total"], int(frac * 100))
    draw.text((pad, bar_y + 38), tally, font=sub_font or plain, fill=C_DIM)

    # What needs the captain. Each row leads with the store's icon and the
    # bead hash in the store's colour -- '[bead-hash]' alone, no duplicated
    # store prefix -- then the title in body text and the reason dimmed.
    # The reason is the actionable part: never truncate it, fit the
    # title into whatever width is left.
    draw.text((pad, 500), "NEEDS THE CAPTAIN", font=lab_font or plain,
              fill=C_STALLED)
    try:
        styles, _, _ = style_mod.load_store_styles(
            (cfg or {}).get(style_mod.PARAM_KEY))
    except Exception:
        styles = {}
    icon_fonts = {}

    def _icon_font(family):
        if not family:
            return row_font
        if family not in icon_fonts:
            try:
                path = screen.font_path(family)
            except Exception:
                path = None
            if path is None:
                icon_fonts[family] = row_font
            else:
                try:
                    from PIL import ImageFont as _IF
                    icon_fonts[family] = _IF.truetype(path, ROW_SIZE)
                except Exception:
                    icon_fonts[family] = row_font
        return icon_fonts[family]

    y = 562
    for item in snap["attention"]:
        issue, reason = item["issue"], item["reason"]
        try:
            color, icon, _ = style_mod.style_for(issue["store"], styles)
        except Exception:
            color, icon = C_TEXT, style_mod.DEFAULT_ICON
        try:
            entry = (styles or {}).get(str(issue["store"]).lower()) or {}
            glyph_font = _icon_font(entry.get("font"))
        except Exception:
            glyph_font = row_font
        lead = "%s %s " % (icon, style_mod.format_tag(issue))
        icon_txt = "%s " % icon
        tag_txt = "%s " % style_mod.format_tag(issue)
        width = screen.W - 2 * pad - 24  # slack for rasterizer rounding
        # Cap the reason first (~40% of the row), then fit the title
        # into the remainder: the full line always fits the panel.
        reason = _fit(draw, reason, row_font, width * 0.40, max_chars=56)
        tail = " \u2014 " + reason
        title_w = max(120, width - _text_w(draw, lead, row_font)
                      - _text_w(draw, tail, row_font))
        title = _fit(draw, issue["title"], row_font, title_w)
        x = pad
        if row_font is None and glyph_font is None:
            # No TrueType at all: one bitmap-font line, no segmentation.
            draw.text((pad, y), lead + title + tail,
                      font=plain, fill=C_TEXT)
        else:
            try:
                draw.text((x, y), icon_txt, font=glyph_font or plain,
                          fill=color)
                x += _text_w(draw, icon_txt, glyph_font)
                draw.text((x, y), tag_txt, font=row_font or plain,
                          fill=color)
                x += _text_w(draw, tag_txt, row_font)
                draw.text((x, y), title, font=row_font or plain,
                          fill=C_TEXT)
                x += _text_w(draw, title, row_font)
                draw.text((x, y), tail, font=row_font or plain, fill=C_DIM)
            except Exception:
                draw.text((pad, y), lead + title + tail,
                          font=row_font or plain, fill=C_TEXT)
        y += 72
    if not snap["attention"]:
        draw.text((pad, y), "nothing waiting on the captain",
                  font=row_font or plain, fill=C_DIM)

    # Footer: per-store open counts + source.
    parts = []
    for store in sorted(snap["stores"]):
        stat = snap["stores"][store]
        parts.append("%s %d/%d" % (store, stat["open"], stat["total"]))
    foot = "  \u00b7  ".join(parts)
    if source:
        foot += "   (%s)" % source
    if health in ("stale", "error") and error:
        foot += "   [last poll failed: %s]" % error
    draw.text((pad, screen.H - 56),
              _fit(draw, foot, small_font, screen.W - 2 * pad),
              font=small_font or plain, fill=C_DIM)
