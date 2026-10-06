"""The red rule and the readable message under it, for a failed render.

One helper, two callers: the html view and the picker (which renders
through the same engine). Neither may use ImageDraw in its own file --
the picker is a template surface now, and a drawing import there would
mean the Pillow path was never really removed -- so the failure card
lives here instead.

A view that silently shows nothing is indistinguishable from a dead
panel, and every failure here (no template root, missing native library,
unknown variable) is operator-fixable, so each one says what to do about
it instead of just what went wrong.

The colours are the palette's alert family (`renderers/theme.py`), not
literals: this card is one of the places the Pillow path and the
templates show the same role, so the red has to have one owner.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import ImageDraw

import _html_native
import theme


def _columns(screen, margin, font):
    """Rough character budget per line, from a probe of the real face."""
    probe = font.getbbox("M")[2] - font.getbbox("M")[0]
    return max(12, (screen.W - 2 * margin) // max(1, probe))


def _line_height(font):
    box = font.getbbox("Ay")
    return (box[3] - box[1]) + 4


def _wrap(text, columns):
    """Greedy whitespace wrap: these strings are short and read by a human."""
    lines, current = [], ""
    for word in str(text).split():
        candidate = (current + " " + word).strip()
        if len(candidate) > columns and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def error_strip(screen, rect, title, detail):
    """The same failure card, confined to a strip of the panel.

    A sub-view that is composited into a bigger frame (the home screen's
    dock) has no business painting a full-screen card: that would hide the
    tiles around it. This draws the red rule and the message inside
    `rect` only, so the rest of the frame is untouched and the failure is
    still loud where the user is already looking.
    """
    x, y, w, h = (int(v) for v in rect)
    img = screen.new_image(theme.rgb("alert-page"))
    draw = ImageDraw.Draw(img)
    rule = max(4, h // 24)
    draw.rectangle([x, y, x + w, y + rule], fill=theme.rgb("alert"))
    margin = max(8, w // 74)
    title_font = _html_native.ui_font(max(14, h // 8), bold=True)
    body_font = _html_native.ui_font(max(11, h // 12), bold=False)
    probe = body_font.getbbox("M")
    columns = max(12, (w - 2 * margin) // max(1, probe[2] - probe[0]))
    line_y = y + rule + max(6, h // 32)
    for line in _wrap(title, columns)[:2]:
        draw.text((x + margin, line_y), line, font=title_font,
                  fill=theme.rgb("alert-ink"))
        line_y += _line_height(title_font)
    for line in _wrap(detail, columns)[:2]:
        if line_y > y + h:
            break
        draw.text((x + margin, line_y), line, font=body_font,
                  fill=theme.rgb("alert-body"))
        line_y += _line_height(body_font)
    return img


def error_frame(screen, title, detail):
    """Loud, readable failure on the panel -- never a blank, never a crash."""
    img = screen.new_image(theme.rgb("alert-page"))
    draw = ImageDraw.Draw(img)
    margin = max(20, screen.W // 26)
    title_font = _html_native.ui_font(max(20, screen.H // 18), bold=True)
    body_font = _html_native.ui_font(max(16, screen.H // 30), bold=False)
    draw.rectangle([0, 0, screen.W, max(8, screen.H // 48)],
                   fill=theme.rgb("alert"))
    y = margin
    for line in _wrap(title, _columns(screen, margin, title_font))[:3]:
        draw.text((margin, y), line, font=title_font,
                  fill=theme.rgb("alert-ink"))
        y += _line_height(title_font) + 6
    y += 8
    for line in _wrap(detail, _columns(screen, margin, body_font))[:5]:
        draw.text((margin, y), line, font=body_font,
                  fill=theme.rgb("alert-body"))
        y += _line_height(body_font) + 4
    return img