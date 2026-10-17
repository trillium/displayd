"""A review bead as a decision: parse once, deterministically, no AI.

Single concept: turning the JSON of one ``review`` store bead (``review show
<id> --json``: a one-element list, or the bare object) into the model the
panel draws. Pure functions, no store, no daemon.

Options come from, in order:

1. ``metadata.decision.options`` -- the structured form: a list of strings or
   ``{id, label, text?, action?, value?}`` objects. ``action`` is ``respond``
   (default) or ``dismiss``; ``value`` is the answer text sent for a respond.
2. the description's own enumeration -- ``Options: (1) ... (2) ...`` -- which is
   how firstmate holds are filed today. Its answer text is ``Option N: <text>``.

A bead whose text has neither is still shown (context only, no buttons): the
panel never invents choices. The relay boilerplate (``Answer with:``,
``correlationId:``, ``The answer is relayed...``) is dropped from the context.
"""

import re

MAX_OPTIONS = 5
LABEL_CHARS = 78
ACTIONS = ("respond", "dismiss")
ID_RE = re.compile(r"^[a-z][a-z0-9_]*-[a-z0-9]{2,32}$")
BOILERPLATE = ("answer with:", "correlationid:", "the answer is relayed")
_MARK = re.compile(r"\((\d)\)\s*")
_TAIL = re.compile(r"[\s;,]+(?:or\s*)?$")


def fit(text, chars):
    text = " ".join(str(text).split())
    return text if len(text) <= chars else text[:max(1, chars - 1)].rstrip() + "…"


def context_of(description):
    """The description minus the relay boilerplate lines, blanks collapsed."""
    lines = [ln.strip() for ln in str(description or "").splitlines()]
    keep = [ln for ln in lines if ln and not ln.lower().startswith(BOILERPLATE)]
    return "\n".join(keep)


def _enumerated(text):
    """``(1) a; (2) b`` -> ['a', 'b']; [] unless 1..n are all present in order."""
    marks = list(_MARK.finditer(text))
    nums = [int(m.group(1)) for m in marks]
    if len(nums) < 2 or nums != list(range(1, len(nums) + 1)):
        return []
    out = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = _TAIL.sub("", text[mark.end():end].replace("\n", " ").strip())
        if i + 1 == len(marks):  # the last one runs to its sentence end
            body = body.split(". ")[0].rstrip(".")
        out.append(body.strip())
    return [b for b in out if b]


def _option(index, raw):
    """One normalized option from a structured entry (or None if unusable)."""
    if isinstance(raw, str):
        raw = {"label": raw}
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or raw.get("label") or "").strip()
    label = str(raw.get("label") or text).strip()
    action = raw.get("action", "respond")
    if not label or action not in ACTIONS:
        return None
    oid = str(raw.get("id") or index)
    value = str(raw.get("value") or text or label)
    return {"id": oid, "label": fit(label, LABEL_CHARS), "text": text or label,
            "action": action, "value": value}


def _structured(meta):
    decision = meta.get("decision") if isinstance(meta, dict) else None
    raw = decision.get("options") if isinstance(decision, dict) else None
    if not isinstance(raw, list):
        return []
    found = [_option(i + 1, item) for i, item in enumerate(raw)]
    found = [o for o in found if o]
    return found if len({o["id"] for o in found}) == len(found) else []


def _fallback(description):
    return [{"id": str(i + 1), "label": fit(text, LABEL_CHARS), "text": text,
             "action": "respond", "value": "Option %d: %s" % (i + 1, text)}
            for i, text in enumerate(_enumerated(context_of(description)))]


def parse_bead(bead):
    """The decision model of a bead JSON; ``ValueError`` when it is not one."""
    if isinstance(bead, list):
        bead = bead[0] if len(bead) == 1 else None
    if not isinstance(bead, dict) or not ID_RE.match(str(bead.get("id") or "")):
        raise ValueError("bead: expected one review bead object with an id")
    labels = [str(x) for x in (bead.get("labels") or [])]
    options = _structured(bead.get("metadata")) or _fallback(bead.get("description"))
    options = options[:MAX_OPTIONS]
    status = str(bead.get("status") or "")
    reason = None
    if status != "open":
        reason = "bead is %s, not open" % (status or "unknown")
    elif "human" not in labels:
        reason = "not flagged for a human (no 'human' label)"
    elif not options:
        reason = "no options to choose from"
    return {"id": bead["id"], "title": str(bead.get("title") or bead["id"]),
            "context": context_of(bead.get("description")), "status": status,
            "labels": labels, "options": options, "actionable": reason is None,
            "reason": reason,
            "structured": bool(_structured(bead.get("metadata")))}
