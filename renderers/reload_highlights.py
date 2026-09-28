"""Bounded highlights for the reload confirmation view.

"Highlights" is mechanical, never a model call: the commit subject line
(first non-empty line, guaranteed) plus up to three of the most
meaningful body lines -- bullet lines (`-`, `*`, `•`, `1.`) win over
plain prose, and trailer lines (`Signed-off-by:`, `Co-authored-by:`,
`Fixes:`, ...) never count. Short and large on the panel beats complete.

Two call sites share this file so the rule cannot drift:
  * `deploy.sh` runs it as a script (`... | python3 reload_highlights.py`)
    on the Mac -- where git works -- to extract the deployed commit's
    message into a bounded highlights string. The lnx-server host cannot
    read git history (its `.git` is a gitdir pointer at a MacBook path),
    so extraction must happen Mac-side and travel with `POST /reload`.
  * `displayd.reload()` and `renderers/reload.py` use `sanitize()` on the
    same bounds, so a missing/malformed field degrades to today's
    SHA+QR+hint view instead of breaking it.

The highlights string never touches the QR payload: the renderer encodes
only relay-shaped URLs or the commit page, exactly as before. Caller text
is bounded hard (lines, per-line, total) and stripped of control
characters, so this is not a general "draw arbitrary caller text"
surface -- it is deploy text on a deploy-proof view, and the full SHA
stays prominent whatever highlights carry.
"""

import re
import sys

# Wire bounds: short enough to draw large beside an unscannable-small QR.
MAX_LINES = 5
MAX_LINE = 80
MAX_TOTAL = 280

# Extraction bounds: subject guaranteed, a few body lines at most.
SUBJECT_MAX = 100
BODY_MAX = 100
BODY_LINES = 3

CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s")
BULLET_RE = re.compile(r"^(?:[-*•]|\d+[.)])\s+(.*)$")


def sanitize(text):
    """Bounded clean highlights, or None when missing/malformed/empty.

    Never raises: non-strings, blank strings, and strings that clean down
    to nothing all return None, and the caller renders the classic
    SHA+QR+hint view. Control characters are stripped, over-long lines
    cut, line count and total length capped. Idempotent."""
    if not isinstance(text, str):
        return None
    text = CONTROL_RE.sub("", text.replace("\r", "\n").replace("\t", " "))
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln[:MAX_LINE].rstrip() for ln in lines if ln][:MAX_LINES]
    out = "\n".join(lines)
    if len(out) > MAX_TOTAL:
        out = out[:MAX_TOTAL].rstrip()
    return out or None


def extract(message):
    """A commit message (`git log --format=%B`) to a highlights string.

    Subject line first (guaranteed -- even a subject-only commit yields
    just the subject), then up to BODY_LINES body lines with bullets
    preferred over prose and trailers skipped. Output already satisfies
    sanitize() bounds; empty input yields "" (the caller then omits the
    field and the panel shows today's view)."""
    if not isinstance(message, str):
        return ""
    raw = [ln.strip() for ln in message.splitlines()]
    raw = [ln for ln in raw if ln]
    if not raw:
        return ""
    subject = raw[0][:SUBJECT_MAX].rstrip()
    bullets, prose = [], []
    for line in raw[1:]:
        if TRAILER_RE.match(line) or line.startswith("#"):
            continue
        bullet = BULLET_RE.match(line)
        if bullet and bullet.group(1).strip():
            bullets.append(bullet.group(1).strip())
        else:
            prose.append(line)
    # Bullets are the meaningful lines; without them, the first prose
    # lines (the "first sentence" of the body) stand in.
    body = (bullets or prose[:2])[:BODY_LINES]
    body = [ln[:BODY_MAX].rstrip() for ln in body if ln]
    return sanitize(subject + "\n" + "\n".join(body)) or subject


def load_font(screen, name, size):
    """A truetype font via the screen, or None when unavailable."""
    try:
        from PIL import ImageFont
        path = screen.font_path(name)
    except Exception:
        return None
    if path is None:
        return None
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return None


def fit_font(screen, draw, name, size, text, max_frac,
             min_size, step):
    """Shrink `name` from `size` until `text` fits `max_frac` of the width.

    Returns the fitted font (or None when no font loads). Never raises
    and never clips: worst case the minimum size draws and the caller
    decided that text was short enough to survive it."""
    font = load_font(screen, name, size)
    if font is None:
        return None
    try:
        px = size
        while (px > min_size
               and draw.textlength(text, font=font) > screen.W * max_frac):
            px -= step
            font = load_font(screen, name, px)
            if font is None:
                break
    except Exception:
        pass
    return font


def wrap(draw, font, text, max_px):
    """Greedy word-wrap one paragraph to pixel width. Never raises."""
    words, lines, current = text.split(), [], ""
    try:
        for word in words:
            probe = (current + " " + word).strip()
            if current and draw.textlength(probe, font=font) > max_px:
                lines.append(current)
                current = word
            else:
                current = probe
        if current:
            lines.append(current)
    except Exception:
        return [text] if text else []
    return lines or ([text] if text else [])


HL_SIZE = 56
HL_MIN = 30


def draw(draw_obj, screen, text, x0, x1, y0, y1):
    """Draw sanitized highlights left-aligned in the (x0,x1,y0,y1) box.

    First line (the subject) draws white and bold, the rest light grey;
    the whole block shrinks from HL_SIZE until it fits both dimensions
    and vertically centers in the box. At most six wrapped lines ever
    draw -- beyond that the text was wrong for a glanced-at panel, and
    sanitize() should have caught it upstream."""
    try:
        from PIL import ImageFont  # noqa: F401 (ensures imaging available)
    except Exception:
        return
    paras = (text or "").split("\n")
    size = HL_SIZE
    fitted = None
    while size >= HL_MIN:
        bold = load_font(screen, "DejaVuSans-Bold", size)
        plain = load_font(screen, "DejaVuSans", size) or bold
        # No fonts at all (e.g. a Mac without DejaVu): fall back to
        # PIL's default bitmap font rather than drawing nothing -- the
        # text still shows, just smaller than the panel's large type.
        wrapped = []
        for i, para in enumerate(paras):
            wrapped.extend(wrap(draw_obj, bold if i == 0 else plain,
                                para, x1 - x0))
        wrapped = wrapped[:6]
        try:
            line_h = int(size * 1.28)
            widths = [draw_obj.textlength(
                ln, font=bold if i == 0 else plain)
                for i, ln in enumerate(wrapped)]
        except Exception:
            size -= 4
            continue
        if (max(widths or [0]) <= x1 - x0
                and len(wrapped) * line_h <= y1 - y0):
            fitted = (bold, plain, wrapped, line_h)
            break
        size -= 4
    if fitted is None:
        return
    bold, plain, wrapped, line_h = fitted
    y = y0 + (y1 - y0 - len(wrapped) * line_h) // 2
    for i, line in enumerate(wrapped):
        draw_obj.text((x0, y), line,
                      font=bold if i == 0 else plain,
                      fill=(255, 255, 255) if i == 0 else (200, 200, 205),
                      anchor="la")
        y += line_h


def main():
    """Script entry for deploy.sh: commit message on stdin, highlights out.

    Prints nothing when the message yields no highlights, so the caller
    can omit the field and the panel renders today's view."""
    out = extract(sys.stdin.read())
    if out:
        print(out)


if __name__ == "__main__":
    main()
