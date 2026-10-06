"""Deploy confirmation for the lnx-server jumbotron.

STATIC: drawn once, then parked. The daemon shows this view as a transient
(`POST /reload`) and returns to whatever was showing, so the panel proves
which commit is live without operator follow-up.

The screen carries four things -- the word RELOADED, the full deployed
commit SHA, a QR code, and a short confirm hint -- plus, when the deploy
supplied one, a bounded highlights summary of the commit message beside
the code. The QR encodes a panel-served one-time relay URL (`relay_url`,
`GET /r/<token>`) whenever the daemon supplies one: scanning it
302-redirects the scanner to the commit page AND confirms the view (the
panel returns early). A tap on the panel while this view shows confirms
the same way. When no relay URL is supplied (the panel binds loopback,
unreachable from the captain's phone -- see displayd.relay_base_url),
the QR falls back to the commit page
`https://github.com/trillium/displayd/commit/<full-sha>` and the hint
says tap-only plainly. Either way the payload is derived server-side:
the renderer takes only the SHA (plus the daemon's relay URL) and never
a caller-supplied URL, so a request cannot smuggle in an arbitrary QR
payload -- only relay-shaped URLs (`/r/<token>`) are ever encoded.
Highlights text is caller-supplied but never URL-shaped into the code:
it is sanitised and bounded hard (see reload_highlights), draws as plain
text only, and a missing/malformed field renders today's four-element
view unchanged.

Encoding/drawing reuse the shared pieces (renderers/qr_common.py for the
code, renderers/reload_highlights.py for fonts and highlights): the
vendored Nayuki generator, integer module scaling, and a 4-module quiet
zone, black on white regardless of the panel's dark theme.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qr_common
import reload_highlights
import theme
from _qrcodegen import QrCode

from PIL import ImageDraw

NAME = "reload"
DESCRIPTION = "Deploy confirmation: RELOADED + commit SHA + commit QR (transient)"
STATIC = True
PARAMS = {
    "sha": {"type": "string", "required": True,
            "help": "full 40-character deployed commit SHA; the QR encodes "
                    "the panel-served scan relay for its commit page"},
    "relay_url": {"type": "string",
            "help": "daemon-issued one-time scan relay (GET /r/<token>); "
                    "when absent the QR falls back to the commit page "
                    "and confirmation is tap-only"},
    "highlights": {"type": "string",
            "help": "bounded highlights summary of the commit message "
                    "(subject + a few body lines, no model involved); "
                    "sanitised and capped server-side, drawn as plain "
                    "text only, never encoded into the QR"},
}

TITLE = "RELOADED"

# The exact commit URL rule: the QR payload is always this prefix plus the
# full SHA. displayd.py repeats the prefix for request validation and
# tests/test_reload.py asserts the two agree, so the rule cannot drift.
COMMIT_URL_PREFIX = "https://github.com/trillium/displayd/commit/"

# A relay URL is accepted for encoding only in this shape: an http(s)
# URL whose path is exactly /r/<one-time-token>. Anything else -- a
# commit page, a homepage, an attacker's URL smuggled in as relay_url,
# data, url, or caption -- is ignored and the commit URL is encoded
# instead. displayd.py issues tokens matching RELOAD_TOKEN_RE; the shape
# here mirrors it so renderer and daemon cannot drift apart.
RELAY_URL_RE = re.compile(r"^https?://[^/]+/r/[A-Za-z0-9_-]{16,64}$")

HINT_RELAY = "scan the code or tap the panel to confirm"
HINT_TAP_ONLY = "tap the panel to confirm (scan relay unavailable)"

# A deployed commit SHA: full 40 hex characters, nothing else. Short SHAs,
# branch names, and URLs are all malformed here -- the screen must name the
# exact commit, and the QR must point at its page.
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

TITLE_SIZE = 130
SHA_SIZE = 46
load_font = reload_highlights.load_font
fit_font = reload_highlights.fit_font


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
    """The fallback QR payload for a validated SHA: its commit page."""
    return COMMIT_URL_PREFIX + validate_sha(sha)


def qr_payload(sha, relay_url=None):
    """(payload, via_relay): what the QR encodes for this view.

    The daemon-issued relay URL wins when it has the exact /r/<token>
    shape; every other input -- missing, malformed, or a smuggled generic
    URL -- falls back to the commit page. Returns which path was taken so
    the view can label itself honestly (scan hint vs tap-only note)."""
    sha = validate_sha(sha)
    if (isinstance(relay_url, str) and RELAY_URL_RE.match(relay_url.strip())):
        return relay_url.strip(), True
    return COMMIT_URL_PREFIX + sha, False


def _prompt(screen, title, body):
    """Sensible placeholder: never a crash, never a blank panel."""
    bg = (10, 10, 14)
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)
    title_font = load_font(screen, "DejaVuSans-Bold", 110)
    body_font = load_font(screen, "DejaVuSans", 48)
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
    url, via_relay = qr_payload(sha, params.get("relay_url"))
    hint = HINT_RELAY if via_relay else HINT_TAP_ONLY
    # Sanitised beside the daemon: missing/malformed degrades to today's
    # four-element view, and the string never reaches the QR payload.
    highlights = reload_highlights.sanitize(params.get("highlights"))

    try:
        qr = qr_common.encode(url, QrCode.Ecc.MEDIUM)
    except ValueError:
        # Unreachable for a fixed-shape relay/commit URL, but a clear
        # message beats an unscannable smear if encoding ever fails.
        _prompt(screen, TITLE, "could not encode the commit QR")
        return

    accent = theme.accent_rgb(NAME)
    bg = (10, 10, 14)
    fg = (255, 255, 255)
    img = screen.new_image(bg)
    draw = ImageDraw.Draw(img)

    # Accent bar across the top: the glanceable bit from across the room.
    draw.rectangle([0, 0, screen.W, 18], fill=accent)

    title_font = fit_font(screen, draw, "DejaVuSans-Bold", TITLE_SIZE,
                          TITLE, 0.86, 40, 8)
    title_y = int(screen.H * 0.14)
    draw.text((screen.W // 2, title_y), TITLE,
              font=title_font, fill=fg, anchor="mm")

    # The QR symbol: as large as fits between the title and the SHA line,
    # integer module scaling only (qr_common never smooth-scales). With
    # highlights the code takes the left half at full height-bounded size
    # -- never smaller than scannable -- and the text takes the right;
    # without them the code stays centered exactly as before.
    qr_top = int(screen.H * 0.24)
    qr_bottom = int(screen.H * 0.82)
    qr_half = screen.W // 2 if highlights else screen.W
    target = min(qr_half - 80, qr_bottom - qr_top)
    scale, actual = qr_common.fit_scale(qr, max(120, target))
    symbol = qr_common.render_symbol(qr, scale=scale)
    img.paste(symbol, ((qr_half - actual) // 2, qr_top))
    if highlights:
        reload_highlights.draw(draw, screen, highlights,
                               screen.W // 2 + 40, screen.W - 40,
                               qr_top, qr_bottom)

    # The full SHA under the code, shrunk to fit rather than clipped.
    try:
        probe = ("DejaVuSansMono"
                 if screen.font_path("DejaVuSansMono") else "DejaVuSans")
    except Exception:
        probe = "DejaVuSans"
    sha_font = fit_font(screen, draw, probe, SHA_SIZE, sha, 0.9, 20, 2)
    draw.text((screen.W // 2, int(screen.H * 0.90)), sha,
              font=sha_font, fill=(200, 200, 205), anchor="mm")

    # Confirm hint: how this view clears early (scan and/or tap). Small
    # and shrunk to fit -- it labels the view honestly, never clipped.
    hint_font = fit_font(screen, draw, "DejaVuSans", 30, hint, 0.9, 16, 2)
    draw.text((screen.W // 2, int(screen.H * 0.955)), hint,
              font=hint_font, fill=(140, 140, 150), anchor="mm")

    screen.present(img)
