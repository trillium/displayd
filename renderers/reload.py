"""Deploy confirmation for the lnx-server jumbotron.

STATIC: drawn once, then parked. The daemon shows this view as a transient
(`POST /reload`) and returns to whatever was showing, so the panel proves
which commit is live without operator follow-up.

The screen carries exactly three things: the word RELOADED, the full
deployed commit SHA, and a QR code whose payload is exactly
https://github.com/trillium/displayd/commit/<full-sha> -- the commit page,
never the repository homepage or a caller-supplied URL. The renderer takes
only the SHA; the URL is derived here, so a request cannot smuggle in an
arbitrary QR payload.

Encoding/drawing reuse the shared piece (renderers/qr_common.py): the
vendored Nayuki generator, integer module scaling, and a 4-module quiet
zone, black on white regardless of the panel's dark theme.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qr_common
from _qrcodegen import QrCode

from PIL import ImageDraw, ImageFont

NAME = "reload"
DESCRIPTION = "Deploy confirmation: RELOADED + commit SHA + commit QR (transient)"
STATIC = True
# Playlist progress-bar colour for this view (see playlist.accent_for).
ACCENT = "#50DC78"
PARAMS = {
    "sha": {"type": "string", "required": True,
            "help": "full 40-character deployed commit SHA; the QR encodes "
                    "its github.com/trillium/displayd commit page"},
}

TITLE = "RELOADED"

# The exact commit URL rule: the QR payload is always this prefix plus the
# full SHA. displayd.py repeats the prefix for request validation and
# tests/test_reload.py asserts the two agree, so the rule cannot drift.
COMMIT_URL_PREFIX = "https://github.com/trillium/displayd/commit/"

# A deployed commit SHA: full 40 hex characters, nothing else. Short SHAs,
# branch names, and URLs are all malformed here -- the screen must name the
# exact commit, and the QR must point at its page.
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

TITLE_SIZE = 130
SHA_SIZE = 46


def validate_sha(sha):
    """Normalise a deployed SHA or raise ValueError with a clear reason.

    Returns the lowercase SHA. Missing, non-string, wrong-length
    (including excessive), and non-hex input are all rejected -- the
    caller maps ValueError to HTTP 400."""
    if sha is None or (isinstance(sha, str) and not sha.strip()):
        raise ValueError("sha is required: post the full 40-character "
                         "deployed commit SHA")
    if not isinstance(sha, str):
        raise ValueError("sha must be a string, got %s"
                         % type(sha).__name__)
    if len(sha) != 40:
        raise ValueError("sha must be a full 40-character commit SHA, "
                         "got %d characters" % len(sha))
    if not SHA_RE.match(sha):
        raise ValueError("sha must be hexadecimal "
                         "(0-9, a-f): %r is not a commit SHA" % sha)
    return sha.lower()


def commit_url(sha):
    """The exact QR payload for a validated SHA."""
    return COMMIT_URL_PREFIX + validate_sha(sha)


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


def _prompt(screen, title, body):
    """Sensible placeholder: never a crash, never a blank panel."""
    bg = (10, 10, 14)
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    title_font = _font(screen, "DejaVuSans-Bold", 110)
    body_font = _font(screen, "DejaVuSans", 48)
    draw.multiline_text(
        (screen.W // 2, screen.H // 2 - 40), title,
        font=title_font or body_font, fill=(255, 255, 255),
        anchor="mm", align="center",
    )
    if body:
        draw.multiline_text(
            (screen.W // 2, screen.H // 2 + 120), body,
            font=body_font or title_font, fill=(200, 200, 205),
            anchor="ma", align="center", spacing=10,
        )
    screen.present(img)


def run(screen, params, stop):
    params = params or {}
    try:
        sha = validate_sha(params.get("sha"))
    except ValueError as exc:
        _prompt(screen, TITLE, str(exc))
        return
    url = COMMIT_URL_PREFIX + sha

    try:
        qr = qr_common.encode(url, QrCode.Ecc.MEDIUM)
    except ValueError:
        # Unreachable for a fixed-shape commit URL, but a clear message
        # beats an unscannable smear if encoding ever fails.
        _prompt(screen, TITLE, "could not encode the commit QR")
        return

    accent = screen.color(ACCENT, (80, 220, 120))
    bg = (10, 10, 14)
    fg = (255, 255, 255)
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)

    # Accent bar across the top: the glanceable bit from across the room.
    draw.rectangle([0, 0, screen.W, 18], fill=accent)

    title_font = _font(screen, "DejaVuSans-Bold", TITLE_SIZE)
    if title_font is not None:
        try:
            size_px = TITLE_SIZE
            while (size_px > 40 and
                   draw.textlength(TITLE, font=title_font) > screen.W * 0.86):
                size_px -= 8
                title_font = _font(screen, "DejaVuSans-Bold", size_px)
                if title_font is None:
                    break
        except Exception:
            pass
    title_y = int(screen.H * 0.14)
    draw.text((screen.W // 2, title_y), TITLE,
              font=title_font, fill=fg, anchor="mm")

    # The QR symbol: as large as fits between the title and the SHA line,
    # integer module scaling only (qr_common never smooth-scales).
    qr_top = int(screen.H * 0.24)
    qr_bottom = int(screen.H * 0.82)
    target = min(screen.W - 80, qr_bottom - qr_top)
    scale, actual = qr_common.fit_scale(qr, max(120, target))
    symbol = qr_common.render_symbol(qr, scale=scale)
    img.paste(symbol, ((screen.W - actual) // 2, qr_top))

    # The full SHA under the code, shrunk to fit rather than clipped.
    sha_font = (_font(screen, "DejaVuSansMono", SHA_SIZE)
                or _font(screen, "DejaVuSans", SHA_SIZE))
    if sha_font is not None:
        try:
            size_px = SHA_SIZE
            probe_name = ("DejaVuSansMono"
                          if screen.font_path("DejaVuSansMono") else "DejaVuSans")
            while (size_px > 20 and
                   draw.textlength(sha, font=sha_font) > screen.W * 0.9):
                size_px -= 2
                sha_font = _font(screen, probe_name, size_px)
                if sha_font is None:
                    break
        except Exception:
            pass
    draw.text((screen.W // 2, int(screen.H * 0.90)), sha,
              font=sha_font, fill=(200, 200, 205), anchor="mm")

    screen.present(img)
