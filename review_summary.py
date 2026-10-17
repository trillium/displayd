"""Optional AI summary of a review bead's *unstructured* context. OFF by default.

Single concept: the one place a model may touch the decision page, and the
rules that fence it in. The page itself is deterministic (review_decision.py
parses, review_view.py draws); this only offers a shorter reading of a long,
free-text context. Options, labels, ids and the answer text are never passed
to it and never come from it.

Off unless ``DISPLAYD_REVIEW_AI=1`` *and* a provider has been registered with
``set_provider`` (a ``fn(text) -> str``; none ships with displayd, so the
default install has no model call at all). Only a bead without structured
options, and with a context longer than ``MIN_CHARS``, is summarized. Any
provider failure leaves the plain context. A summary is shown labelled
``AI summary:`` and bounded to ``MAX_CHARS``.
"""

import os

ENV = "DISPLAYD_REVIEW_AI"
MIN_CHARS = 400
MAX_CHARS = 400
LABEL = "AI summary: "

_provider = None


def set_provider(fn):
    """Register (or clear with None) the summarizer: ``fn(context) -> str``."""
    global _provider
    _provider = fn


def enabled(env=None):
    return (os.environ if env is None else env).get(ENV) == "1" and _provider is not None


def apply(decision, env=None):
    """``decision`` plus a ``summary`` when the rules allow one; else unchanged."""
    if not enabled(env) or decision.get("structured") \
            or len(decision.get("context", "")) < MIN_CHARS:
        return decision
    try:
        text = " ".join(str(_provider(decision["context"])).split())
    except Exception:
        return decision
    if not text:
        return decision
    return dict(decision, summary=text[:MAX_CHARS])
