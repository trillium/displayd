"""The trust boundary for the html renderer: named local templates only.

The renderer never accepts HTML or CSS from a caller. A caller names a
template that already exists on disk, and passes *data* as variables that
are HTML-escaped on the way in. So a feed payload -- the one part of a
panel that is genuinely untrusted -- can never introduce markup, a style,
a remote URL, or a script.

Layout of the contract:

- the root directory comes from the environment, never from a request:
  ``$DISPLAYD_HTML_TEMPLATES`` or ``<repo>/html-templates``
- a name is ``[A-Za-z0-9_-]+.html`` -- no path separators, no traversal,
  no nested directories, no extension tricks
- a template is a single file; it may reference sibling *images* (the
  native layer refuses anything that escapes the root)
- ``{{name}}`` is the substitution for data, and every such value is
  escaped
- ``{{name|raw}}`` is the ONE markup slot, and it can only be filled by
  a renderer, never by a caller: it takes a separate ``raw`` mapping that
  the html renderer does not have, so a pushed value can never reach it
  (see ``load``)
- a placeholder with no value is an error, not a silent blank: the view
  then names the missing key instead of quietly showing a wrong panel
- HTML comments are dropped before placeholders are looked for, so a
  commented-out example or a disabled section costs no variables
"""

import os
import re

import _html_compose
import _html_native

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.html$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}")
RAW_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\|\s*raw\s*\}\}")
RAW_MARKER = "|raw"
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
MAX_TEMPLATE_BYTES = 256 * 1024
MAX_VALUE_CHARS = 4096
MAX_VARS = 64
MAX_RAW_CHARS = 64 * 1024
NAME_CHARS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ENV_ROOT = "DISPLAYD_HTML_TEMPLATES"
DEFAULT_DIR = "html-templates"

# The five characters that can change a document's meaning. Spelled out
# here rather than using the stdlib html module: this renderer is itself
# named html, and the renderers directory goes on sys.path, so
# `import html` from this file could hand back the renderer instead.
ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
           ('"', "&quot;"), ("'", "&#x27;"))


def escape(text):
    for char, entity in ESCAPES:
        text = text.replace(char, entity)
    return text


class TemplateError(Exception):
    """Anything that makes a template unusable. The view shows the text."""


def hex_colour(color):
    """A palette tuple -> a CSS colour, for a template that takes colours
    in a style attribute.

    The inputs are colours a screen already parsed (or constants in this
    tree), never a caller's own text, so this cannot become a CSS
    injection point: the escaping rule below is about *data* reaching a
    document, and a colour that has been through here is not data.
    """
    return "#%02x%02x%02x" % tuple(int(v) for v in tuple(color)[:3])


def default_root():
    """The one directory templates may come from, symlinks resolved."""
    configured = os.environ.get(ENV_ROOT)
    if configured:
        return _html_native.allow_root(configured)
    here = os.path.dirname(os.path.abspath(__file__))
    return _html_native.allow_root(os.path.join(os.path.dirname(here), DEFAULT_DIR))


def available(root=None):
    """Template names present in the root, sorted. For help text + tests."""
    base = root or default_root()
    try:
        entries = os.listdir(base)
    except OSError:
        return []
    return sorted(e for e in entries if NAME_RE.match(e))


def _as_text(value):
    """Placeholder value -> the string that will be filled in."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value if isinstance(value, str) else str(value)


def _substitute(text, variables, raw=None):
    """Fill {{name}} placeholders, HTML-escaping every value.

    ``{{name|raw}}`` is filled from `raw` instead, unescaped: it is the
    only way markup reaches a document, so it is a separate mapping that
    only a renderer holds. Raw values are refused if they carry a
    placeholder of their own, so the "typo in a placeholder" check below
    still means what it says.
    """
    raw = raw or {}
    for key, value in raw.items():
        if not isinstance(key, str) or not NAME_CHARS_RE.match(key):
            raise TemplateError("bad raw slot name %r" % (key,))
        if not isinstance(value, str):
            raise TemplateError("raw slot %r must be a string" % (key,))
        if "{{" in value or len(value) > MAX_RAW_CHARS:
            raise TemplateError("raw slot %r is not renderable markup" % (key,))
    if len(variables) > MAX_VARS:
        raise TemplateError("too many variables (%d, max %d)"
                            % (len(variables), MAX_VARS))
    # Comments go first. The engine ignores them, so we must too: a
    # commented-out section or a documented example must not look like a
    # missing variable and black out the panel.
    text = COMMENT_RE.sub("", text)
    escaped = {}
    for key, value in variables.items():
        if not isinstance(key, str) or not NAME_CHARS_RE.match(key):
            raise TemplateError("bad variable name %r" % (key,))
        value = _as_text(value)
        if len(value) > MAX_VALUE_CHARS:
            value = value[:MAX_VALUE_CHARS] + "…"
        escaped[key] = escape(value)

    def put_raw(match):
        key = match.group(1)
        if key not in raw:
            raise TemplateError("template needs raw slot %r" % key)
        return raw[key]

    if raw:
        text = RAW_RE.sub(put_raw, text)
    # A raw slot nobody filled is a missing variable like any other, and
    # names itself the same way -- never a silently blank slot.
    text = RAW_RE.sub(lambda m: "{{%s}}" % m.group(1), text)

    def put(match):
        key = match.group(1)
        if key not in escaped:
            raise TemplateError("template needs variable %r" % key)
        return escaped[key]

    filled = PLACEHOLDER_RE.sub(put, text)
    if "{{" in filled:
        # A typo like {{ title }} or {{a-b}} would otherwise reach the
        # engine as literal text, which is a silent wrong panel.
        leftover = filled.split("{{", 1)[1].split("}}", 1)[0].strip()
        raise TemplateError("unusable placeholder {{%s}}" % leftover[:40])
    return filled


def load(name, variables=None, root=None, raw=None):
    """Read one template and fill it in.

    Returns (html_text, root). ``root`` is handed to the native layer as
    the only directory that document may reference images from.

    `raw` fills the ``{{name|raw}}`` slots with unescaped markup. It is a
    separate argument on purpose: a caller of the html renderer supplies
    `variables` and nothing else, so request data can only ever arrive
    escaped. Only a renderer that builds its own document -- the picker,
    for one -- passes `raw`, and what it passes is its own markup over
    values it escaped itself.

    The returned document also carries the design tokens
    (``_html_compose.compose``), so a template can use ``var(--ink)`` and
    friends instead of repeating a colour literal. That block is generated
    in-process from ``theme.py`` and holds no caller data;
    ``_html_compose.strip_tokens`` takes it back off for a test that wants
    the substitution contract exactly.
    """
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise TemplateError("bad template name %r (want letters, digits, _ or -)"
                            % (name,))
    base = _html_native.allow_root(root) if root else default_root()
    path = os.path.join(base, name)
    if os.path.dirname(os.path.realpath(path)) != base:
        raise TemplateError("template %s is outside %s" % (name, base))
    try:
        size = os.path.getsize(path)
    except OSError as err:
        if os.path.isdir(base):
            raise TemplateError("no template %r in %s" % (name, base))
        raise TemplateError("template root %s is missing -- set $%s"
                            % (base, ENV_ROOT))
    if size > MAX_TEMPLATE_BYTES:
        raise TemplateError("template %s is %d bytes (max %d)"
                            % (name, size, MAX_TEMPLATE_BYTES))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as err:
        raise TemplateError("cannot read %s: %s" % (name, err))
    filled = _substitute(text, variables or {}, raw=raw)
    return _html_compose.compose(filled), base
