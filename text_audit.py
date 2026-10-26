"""Drawn text vs its declared box: the offscreen probe behind GET /text/check.

Single concept: run a view's own ``run()`` once, offscreen, on a probe screen
that carries stress feeds, with the text ledger recording (renderers/ui/
textbox.py), and report every line whose ink escaped its declared box. It is
the text twin of touch_audit.py: nothing here knows any view's layout -- the
box is whatever the drawing declared -- so the check cannot drift from the
renderer. The probe never touches the live screen, feed store or framebuffer.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "renderers"))

from ui import textbox  # noqa: E402

from feed_store import FeedStore  # noqa: E402
from framebuffer_headless import HeadlessFramebuffer  # noqa: E402
from screen import Screen  # noqa: E402

NEEDS_ENGINE = ("chat", "html", "options", "picker", "review", "unified")
PROBE_SECONDS = 6.0   # a view that draws nothing in this long is reported
COVERAGE = ("every line a view draws via Pillow, measured as ink; the box is "
            "the draw's own declaration (ui.text.write room/box, ui.textbox."
            "boxed); lines with no box are held to the frame only; litehtml "
            "views draw inside their CSS layout and are reported unchecked "
            "when the engine is absent")


class ProbeScreen(Screen):
    """A screen that keeps its first frame and stops the view."""

    def __init__(self, width, height, feeds, stop):
        super().__init__(HeadlessFramebuffer(width, height))
        self.feeds = feeds
        self._stop = stop

    def present(self, img):
        self._stop.set()


def _feeds(module, feeds):
    store = FeedStore()
    for input_name, spec in (getattr(module, "INPUTS", None) or {}).items():
        store.declare(getattr(module, "NAME", ""), input_name, spec)
    for key, payloads in (feeds or {}).items():
        # "input" feeds the view itself; "view/input" feeds another view's
        # input the drawing reads (the macbook header reads talon_apps).
        owner, _, input_name = key.rpartition("/")
        owner = owner or getattr(module, "NAME", "")
        spec = (getattr(module, "INPUTS", None) or {}).get(input_name) or {}
        for payload in payloads:
            store.push(owner, input_name, payload, spec if owner == getattr(
                module, "NAME", "") else {"type": "object"})
    return store


def probe(module, params, feeds, width, height):
    """One offscreen draw: ``{lines, error}``; lines are ledger entries."""
    stop = threading.Event()
    out = {"lines": [], "error": None}

    def work():
        try:
            screen = ProbeScreen(width, height, _feeds(module, feeds), stop)
            with textbox.recording() as ledger:
                try:
                    module.run(screen, dict(params or {}), stop)
                finally:
                    out["lines"] = list(ledger)
        except Exception as err:
            out["error"] = "%s: %s" % (type(err).__name__, err)
            stop.set()

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(PROBE_SECONDS)
    if worker.is_alive():
        stop.set()
        worker.join(2.0)
        if worker.is_alive():
            out["error"] = out["error"] or "probe did not stop"
    return out


def judge(lines):
    """Entries -> offences, each naming the text, its ink and its box."""
    found = []
    for entry in lines:
        for hit in textbox.violations(entry):
            found.append({"text": entry["text"][:80],
                          "source": str(entry["source"])[:80],
                          "label": entry.get("label"), "ink": list(entry["ink"]),
                          "box": list(entry["box"]) if entry.get("box") else None,
                          "frame": list(entry["frame"]) if entry.get("frame") else None,
                          **hit})
    return found


def check_case(module, case, width, height):
    result = probe(module, case.get("params"), case.get("feeds"), width, height)
    lines = result["lines"]
    offences = judge(lines)
    boxed = sum(1 for e in lines if e.get("box"))
    return {"case": case.get("label", "default"), "drawn": len(lines),
            "boxed": boxed, "offences": offences, "error": result["error"],
            "ok": not offences and not result["error"]}
