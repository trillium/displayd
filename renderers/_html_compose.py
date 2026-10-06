"""The template composition step: from a filled template to a document.

A template file on disk is a *fragment* of a panel document, not the whole
thing: this module is what turns one into the document the engine lays
out. It does two jobs, both of them pure text transforms over trusted
local files, and both applied at load time in ``_html_templates.load``:

1. **the token layer.** The palette in ``theme.py`` reaches a document as
   CSS custom properties, in one ``:root`` block spliced into the head.
   The pinned litehtml revision implements css-variables-2 (``subst_var``
   in ``style.cpp``) and inherits a custom property up the element tree,
   so ``color: var(--ink)`` in a template resolves to exactly
   ``theme.INK``. Without this block every ``var()`` in a template is
   undefined, and litehtml drops the declaration -- which is why a
   template must not carry a colour literal of its own.
   ``strip_tokens()`` exists so a test can assert the substitution
   contract exactly while the token block is present.

2. **the chrome layer.** litehtml has no ``@import`` (CSS import is
   inert) and a template cannot inherit from another, so the shared panel
   chrome used to be copy-pasted into every template that wears it, with a
   test holding the copies in agreement. Instead it is authored ONCE in
   ``html-templates/_chrome.html`` as named sections, and a template names
   the sections it wants with an include directive:

       <!--#include css-->          the chrome stylesheet, in its <style>
       <!--#include head-->         the header band markup
       <!--#include strip-left-->   the left gesture strip
       ...

   ``expand()`` splices those in, so one definition is in force and a
   template that restates a chrome rule has to re-author it by hand
   (``tests/test_html.py`` fails on that). The per-surface differences
   that genuinely exist -- the inset, the title size, which side the title
   leans to -- are not copies: they are custom properties a variant class
   on ``<body>`` sets, and the variants live in the partial with the rules.

Both steps run on trusted files only: a partial name is a fixed constant
(``chrome``), the file is read from the same root as the template itself,
and no caller value is involved in either transform. A caller's data still
arrives only through ``_html_templates._substitute``, escaped.
"""

import os
import re

import theme

MARKER = "/* displayd-tokens */"
BLOCK_RE = re.compile(r"<style>\s*/\* displayd-tokens \*/.*?</style>\n?",
                      re.DOTALL)

HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)
HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)

# ---- the chrome layer -------------------------------------------------

CHROME = "chrome"
INCLUDE_RE = re.compile(r"[ \t]*<!--\s*#include\s+([A-Za-z0-9_.-]{1,64})"
                        r"\s*-->[ \t]*\n?")
SECTION_RE = re.compile(r"[ \t]*<!--\s*#section\s+([A-Za-z0-9_.-]{1,64})\s*-->"
                        r"[ \t]*\n?(.*?)"
                        r"[ \t]*<!--\s*#endsection\s*-->[ \t]*\n?",
                        re.DOTALL)
PARTIAL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Bounds, so a malformed or hostile tree cannot expand without limit: a
# section may include another section, and the depth bound turns a cycle
# into an error instead of a hang.
MAX_INCLUDE_DEPTH = 4
MAX_INCLUDES = 64
MAX_EXPANDED_BYTES = 512 * 1024


class ComposeError(Exception):
    """A document that cannot be assembled: a partial that is missing or
    unreadable, an unknown section name, an include cycle, an oversized
    result. ``_html_templates`` turns this into a TemplateError, which the
    view shows as a card in place of the panel."""


def token_block():
    """The one stylesheet every panel document inherits. Pure."""
    return ("<style>\n%s\n%s\n%s\n</style>" %
            (MARKER, theme.css_root(), theme.slot_root()))


def strip_tokens(text):
    """Take the injected token block back off, leaving the template's text.

    For tests that want the substitution contract exactly: the block is
    fixed decoration, so it is removed rather than asserted around.
    """
    return BLOCK_RE.sub("", text, count=1)


def _insertion_point(text):
    """Where a ``<style>`` element belongs in this document.

    Before ``</head>`` when there is one (every shipped template has one),
    else right after ``<head ...>``, else at the very front -- the HTML
    parser moves a leading style element into the head itself, so a bare
    fragment still gets the tokens.
    """
    match = HEAD_CLOSE_RE.search(text)
    if match:
        return match.start()
    match = HEAD_OPEN_RE.search(text)
    if match:
        return match.end()
    return 0


def compose(text):
    """Add the token layer to an already-filled document. Idempotent."""
    if not isinstance(text, str) or MARKER in text:
        return text
    at = _insertion_point(text)
    return text[:at] + token_block() + "\n" + text[at:]


# ---- the chrome layer -------------------------------------------------


def partial_path(root, partial=CHROME):
    """The one file a partial name may come from. Never a caller's name."""
    if not PARTIAL_NAME_RE.match(partial or ""):
        raise ComposeError("bad partial name %r" % (partial,))
    return os.path.join(root, "_%s.html" % partial)


def partial_sections(root, partial=CHROME):
    """Every named section of one partial, in file order.

    A partial is a ``_``-prefixed template-directory file -- shipped by
    the same ``*.html`` install rule as the templates, and hidden from
    ``available()`` -- whose text is a list of named sections. Anything
    outside a section (its header comment, usually) documents the file and
    is never spliced into a document.
    """
    path = partial_path(root, partial)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as err:
        raise ComposeError("cannot read the %s partial %s: %s"
                           % (partial, path, err))
    sections = {}
    for name, body in SECTION_RE.findall(text):
        if name in sections:
            raise ComposeError("the %s partial defines %r twice"
                               % (partial, name))
        sections[name] = body
    if not sections:
        raise ComposeError("the %s partial %s has no sections"
                           % (partial, path))
    return sections


def expand(text, root, partial=CHROME, depth=0):
    """Replace every include directive with the section it names.

    An unknown section is an error that names itself, not a silently
    absent band: a template that lost its chrome would otherwise reach the
    panel as a plausible-looking wrong screen. A section that includes
    another is expanded too, bounded by ``MAX_INCLUDE_DEPTH`` so a cycle
    fails instead of spinning.
    """
    if not isinstance(text, str) or "#include" not in text:
        return text
    if depth >= MAX_INCLUDE_DEPTH:
        raise ComposeError("include nesting deeper than %d levels"
                           % MAX_INCLUDE_DEPTH)
    sections = partial_sections(root, partial)
    count = []

    def put(match):
        name = match.group(1)
        if name not in sections:
            raise ComposeError("no %s section %r (have: %s)"
                               % (partial, name, ", ".join(sorted(sections))))
        count.append(name)
        if len(count) > MAX_INCLUDES:
            raise ComposeError("more than %d includes in one document"
                               % MAX_INCLUDES)
        return sections[name]

    filled = INCLUDE_RE.sub(put, text)
    if len(filled) > MAX_EXPANDED_BYTES:
        raise ComposeError("composed document is %d bytes (max %d)"
                           % (len(filled), MAX_EXPANDED_BYTES))
    if INCLUDE_RE.search(filled):
        return expand(filled, root, partial, depth + 1)
    return filled
