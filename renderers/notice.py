"""Transient notice card for the policy layer's notification feature.

Shown switch-then-return, never composed: the daemon puts this view on
screen for a configured duration and then restores whatever was showing.
See ISA decision D6 -- this is the cheap approximation, not composition,
and it interrupts what is showing by design.

The card itself is the component layer's ``ui.panel``: an accent bar, a
headline scaled to the panel, the body under it and the severity tag in
the corner. Severity picks the palette role the bar and the tag wear --
``info`` the accent, ``warn`` the attention amber, ``critical`` the alert
red -- and an explicit `color` param wins.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import theme
from ui import panel

NAME = "notice"
DESCRIPTION = "Transient notification card (policy layer shows it, then returns)"
STATIC = True
# Playlist progress-bar colour for this view (see playlist.accent_for).
# A notice only ever shows as a transient, which hides the bar; this is
# the fallback if it is ever parked directly.
ACCENT = "#5AC8FF"
PARAMS = {
    "title": {"type": "string", "required": True, "help": "notice headline"},
    "body": {"type": "string", "help": "longer text, \n starts a new line"},
    "severity": {"type": "string", "help": "info (default), warn, or critical"},
    "color": {"type": "string", "help": "accent colour override; default follows severity"},
    "background": {"type": "string", "help": "background colour, default the panel background"},
}

# Severity -> the palette ROLE it wears. These were three RGB tuples in
# this module; they are the same jobs the alert family already names, so a
# severity is a role lookup here rather than a fourth copy of red/amber.
SEVERITY_ROLE = {
    "info": "accent",
    "warn": "attention",
    "critical": "alert",
}
DEFAULT_TITLE = "(notice)"


def severity_ink(severity):
    """The bar/tag colour for a severity word; unknown reads as ``info``."""
    role = SEVERITY_ROLE.get(str(severity or "").lower(), SEVERITY_ROLE["info"])
    try:
        return theme.rgb(role)
    except Exception:
        return theme.rgb(SEVERITY_ROLE["info"])


def run(screen, params, stop):
    title = str(params.get("title", "") or "").strip() or DEFAULT_TITLE
    body = str(params.get("body", "") or "")
    severity = str(params.get("severity", "info") or "info").lower()
    accent = severity_ink(severity)
    if params.get("color"):
        accent = screen.color(params.get("color"), accent)
    bg = screen.color(params.get("background"), theme.rgb("page"))

    img = screen.new_image(bg)
    # The severity bar is the glanceable bit from across the room; the tag
    # names it for anyone close enough to read.
    panel.card(img, screen, title, body, accent=accent,
               tag_text=severity.upper(), tag_ink=accent)
    screen.present(img)
