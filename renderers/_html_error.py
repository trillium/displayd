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
"""

from PIL import ImageDraw

import _html_native


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


def error_frame(screen, title, detail):
    """Loud, readable failure on the panel -- never a blank, never a crash."""
    img = screen.new_image((28, 10, 14))
    draw = ImageDraw.Draw(img)
    margin = max(20, screen.W // 26)
    title_font = _html_native.ui_font(max(20, screen.H // 18), bold=True)
    body_font = _html_native.ui_font(max(16, screen.H // 30), bold=False)
    draw.rectangle([0, 0, screen.W, max(8, screen.H // 48)], fill=(214, 74, 74))
    y = margin
    for line in _wrap(title, _columns(screen, margin, title_font))[:3]:
        draw.text((margin, y), line, font=title_font, fill=(255, 196, 196))
        y += _line_height(title_font) + 6
    y += 8
    for line in _wrap(detail, _columns(screen, margin, body_font))[:5]:
        draw.text((margin, y), line, font=body_font, fill=(226, 216, 220))
        y += _line_height(body_font) + 4
    return img