"""The template composition step: from a filled template to a document.

A template file on disk is a *fragment* of a panel document, not the whole
thing: this module is what turns one into the document the engine lays
out. Today it does one job -- the **token layer**.

The palette in ``theme.py`` reaches a document as CSS custom properties,
in one ``:root`` block spliced into the head. The pinned litehtml revision
implements css-variables-2 (``subst_var`` in ``style.cpp``) and inherits a
custom property up the element tree, so ``color: var(--ink)`` in a
template resolves to exactly ``theme.INK``. Without this block every
``var()`` in a template is undefined, and litehtml drops the declaration --
which is why a template must not carry a colour literal of its own.

This is a pure text transform over a generated stylesheet: no caller data
is involved, so nothing here widens the trust boundary. ``strip_tokens()``
exists so a test can assert the substitution contract exactly while the
token block is present: the loader's job is still "fill the file, escape
every value", and this is the fixed decoration added around it.

The chrome-sharing step (one shared shell partial spliced in at load time,
because litehtml has no ``@import``) lands here too, on the same footing.
"""

import re

import theme

MARKER = "/* displayd-tokens */"
BLOCK_RE = re.compile(r"<style>\s*/\* displayd-tokens \*/.*?</style>\n?",
                      re.DOTALL)

HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)
HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)


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
