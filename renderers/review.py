"""The review decision page (one review bead, options as touch buttons).

A thin view over ``html-templates/review.html``: the daemon's review flow
(``review_flow.py`` + ``daemon_review.py``) computes the variables and pushes
them; this view only draws them. It has its own name, not the generic ``html``
view's, because touch regions are scoped by the showing view's name
(``view_regions.review``), which is what lets the option rows tap.

POST /review/load {"bead": {...}}      draws it (the daemon shows this view)
POST /feed/review/vars {...}           re-renders in place (the flow does this)

Never takes markup: values are escaped by the template layer, like ``html``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _html_error
import _html_native
import _html_templates as templates

NAME = "review"
DESCRIPTION = "Review bead decision page: context, option rows, confirm (see /review)"
STATIC = False
CAPABILITY = "partial"
TEMPLATE = "review.html"
# bead is REQUIRED on purpose: a renderer without required params joins the live
# picker/home tile set, and a new tile moves every host touch region (see
# tools/regen_touch_tiles.py). The page is reached through /review/load only.
PARAMS = {"bead": {"type": "string", "required": True, "help": "id of the loaded review bead"},
          "vars": {"type": "object", "help": "review.html variables (set by the daemon)"}}
INPUTS = {"vars": {"type": "object", "buffer": 1,
                   "help": "replaces the variables and re-renders"}}
POLL_SECONDS = 0.25
BACKGROUND = (0, 0, 0)
EMPTY = {"eyebrow": "REVIEW", "title": "Review decision", "status": "",
         "context": "No review bead loaded.", "foot_left": "CLOSE", "foot_right": "",
         "o1": "", "o2": "", "o3": "", "o4": "", "o5": ""}


def _draw(screen, variables):
    document, _root = templates.load(TEMPLATE, variables)
    canvas = screen.new_image(BACKGROUND)
    image, _height = _html_native.render(document, screen.W, screen.H,
                                         background=BACKGROUND,
                                         root=templates.default_root())
    canvas.paste(image, (0, 0))
    screen.present(canvas)


def run(screen, params, stop):
    pushed = params.get("vars")
    variables = dict(EMPTY, **pushed) if isinstance(pushed, dict) else dict(EMPTY)
    last = None
    while not stop.is_set():
        fed = screen.get_input(NAME, "vars")
        if fed:
            variables = dict(EMPTY, **fed[-1])
        key = sorted(variables.items())
        if key != last:
            last = key
            try:
                _draw(screen, variables)
            except (templates.TemplateError, _html_native.HtmlRenderError,
                    _html_native.NativeMissing) as err:
                screen.present(_html_error.error_frame(
                    screen, "review: %s" % err, "build it: tools/build_litehtml.sh"))
        stop.wait(POLL_SECONDS)
