"""Render a panel view from a local HTML/CSS template (optional, litehtml).

The point is authoring power: a view that needs real layout, hierarchy and
typography should be a few dozen lines of HTML+CSS in a file, not a few
hundred lines of PIL. litehtml does the layout; PIL does the pixels.

The trust boundary is the whole design. This view never takes markup from
a caller. ``template`` names a file that already exists in the template
root ($DISPLAYD_HTML_TEMPLATES, default html-templates/ next to the
daemon), and every {{variable}} value is HTML-escaped before it reaches
the document. A pushed feed payload can therefore never add a style, a
remote URL, a script, or a file reference. See _html_templates.py.

The native library is optional and lazily loaded, so this view always
loads: without the library it shows what to build instead of going
missing from /renderers. Build it with tools/build_litehtml.sh, or
install the whole runtime set (engine + trusted templates) onto a
target with tools/install_html_runtime.sh, which is what install.sh
and deploy.sh do.

POST /show {"renderer": "html",
            "params": {"template": "status.html", "vars": {...}}}
POST /feed/html/vars {"title": "...", ...}  re-renders in place
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw

import _html_native
import _html_templates as templates

NAME = "html"
DESCRIPTION = ("Render a local HTML/CSS template (litehtml); "
               "template files only, no remote or inline markup")
STATIC = False
PARAMS = {
    "template": {"type": "string", "required": True,
                 "help": "template file name in the template root, e.g. status.html"},
    "vars": {"type": "object", "help": "values for {{placeholders}}, escaped"},
    "background": {"type": "string", "help": "colour under the document, default black"},
}
INPUTS = {
    "vars": {"type": "object", "buffer": 1,
             "help": "replaces the variables and re-renders"},
}

POLL_SECONDS = 0.5
MISSING_LIBRARY_HINT = "build it: tools/build_litehtml.sh"


def _error_frame(screen, title, detail):
    """Loud, readable failure on the panel -- never a blank and never a crash.

    A view that silently shows nothing is indistinguishable from a dead
    panel, and every failure here (no template root, missing native
    library, unknown variable) is operator-fixable, so each one says what
    to do about it instead of just what went wrong.
    """
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


def _compose(screen, document, background):
    """One complete frame: the document, top-aligned, on the panel colour.

    A document taller than the panel is cropped from the top rather than
    scaled -- panel content is read at a glance, and a squashed layout is
    less useful than the first screenful of it.
    """
    canvas = screen.new_image(background)
    image, _content_height = _html_native.render(
        document, screen.W, screen.H, background=background,
        root=templates.default_root())
    canvas.paste(image, (0, 0))
    return canvas


def _key(template, variables, background):
    """Change detector for the poll loop: same inputs, no re-render."""
    try:
        return (template, sorted((str(k), str(v)) for k, v in variables.items()),
                tuple(background))
    except AttributeError:
        return None


def runtime_status():
    """Can this view actually draw, and from what? For GET /state.

    Every path here is the one this renderer really uses, so the answer is
    the install contract rather than a separate opinion about it: the engine
    library this process would dlopen, the engine's own revision string, and
    the template root the trust boundary will read. A target installed
    without tools/install_html_runtime.sh answers ``ok: false`` here with
    the exact path it looked in, instead of the panel discovering it one red
    card at a time.

    Never raises: /state has to stay total, and a status probe that can fail
    is not a status.
    """
    status = {"ok": False, "library": None, "version": None,
              "template_root": None, "templates": 0, "error": None}
    try:
        status["library"] = _html_native.lib_path()
        status["template_root"] = templates.default_root()
        status["templates"] = len(templates.available())
        status["version"] = _html_native.version()
        status["ok"] = bool(status["library"]) and status["templates"] > 0
    except (_html_native.HtmlRenderError, OSError) as err:
        status["error"] = str(err)
    return status


def run(screen, params, stop):
    name = str(params.get("template") or "")
    variables = params.get("vars")
    variables = dict(variables) if isinstance(variables, dict) else {}
    background = tuple(screen.color(params.get("background"), (0, 0, 0)))

    # A bad first paint is the case that matters: report it, then keep the
    # view alive so a later push of good variables can recover it.
    last = object()
    while not stop.is_set():
        pushed = screen.get_input(NAME, "vars")
        if pushed:
            variables = dict(pushed[-1])
        key = _key(name, variables, background)
        if key != last:
            last = key
            try:
                document, _root = templates.load(name, variables)
                screen.present(_compose(screen, document, background))
            except templates.TemplateError as err:
                screen.present(_error_frame(screen, "html: " + str(err),
                                            MISSING_LIBRARY_HINT
                                            if "root" in str(err) else
                                            "fix the template, then re-show"))
            except _html_native.HtmlRenderError as err:
                screen.present(_error_frame(screen, "html: " + str(err),
                                            "template parsed but would not draw"))
            except _html_native.NativeMissing as err:
                screen.present(_error_frame(screen, "html: " + str(err),
                                            MISSING_LIBRARY_HINT))
        stop.wait(POLL_SECONDS)
