"""The failure card: the panel component wearing the alert family.

One definition, two regions -- the whole panel (a view that could not
render) and a rect of a bigger frame (the home screen's dock, which has
no business hiding the tiles around it). Both are ``ui.panel`` with the
alert inks, so "what went wrong" is drawn by the same component that
draws every other card, and the card's own wrap/fit rule applies to a
failure message exactly as it does to any other body.

Neither the picker nor the html view may import a drawing primitive of
its own -- the picker is a template surface now, and a drawing import
there would mean the Pillow path was never really removed -- so this
adapter lives outside the component layer and composes it.

A view that silently shows nothing is indistinguishable from a dead
panel, and every failure here (no template root, missing native library,
unknown variable) is operator-fixable, so each one says what to do about
it instead of just what went wrong.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import theme
from ui import panel as ui_panel
from ui import text as ui_text


def _wrapped(screen, text):
    """The card's body, broken to the room the card will give it."""
    return "\n".join(ui_text.wrap(screen, text, ui_panel.BODY_SIZE,
                                  int(screen.W * ui_panel.MARGIN),
                                  rows=None))


def error_strip(screen, rect, title, detail):
    """The same failure card, confined to a strip of the panel.

    A sub-view that is composited into a bigger frame (the home screen's
    dock) has no business painting a full-screen card: that would hide the
    tiles around it. This draws the alert rule and the message inside
    `rect` only, so the rest of the frame is untouched and the failure is
    still loud where the user is already looking.
    """
    return ui_panel.strip(screen.new_image(theme.rgb("alert-page")), screen,
                          rect, title, detail)


def error_frame(screen, title, detail):
    """Loud, readable failure on the panel -- never a blank, never a crash."""
    return ui_panel.card(
        screen.new_image(theme.rgb("alert-page")), screen, title,
        _wrapped(screen, detail), ink=theme.rgb("alert-ink"),
        body_ink=theme.rgb("alert-body"), accent=theme.rgb("alert"))
