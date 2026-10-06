"""Contract double for the Talon-side ``displayd_apps`` module.

The real implementation lives OUTSIDE this repo, on the Talon install
(``~/.talon/user/trillium_talon/core/displayd_apps/displayd_apps.py``),
so a host without Talon cannot import it. This file is the minimal
module that satisfies the contract ``tests/test_talon_apps.py``
pins, so the contract is executable on every host.

WHAT THIS DOES NOT PROVE: that the installed Talon module behaves like
this. The fixture is a spec of what the bridge and the daemon expect,
nothing more -- a drift between fixture and real install is invisible
here. The same assertions therefore also run against the real module
whenever it is present (``TalonSideTest``, which skips with a reason
naming the missing path).

Talon-side rules this file encodes (all pure, no I/O, no Talon API):

* names are stripped, non-printable-free and capped at ``NAME_CHARS``;
* the state document is deduped, sorted and capped at ``MAX_APPS``;
* a request older than ``REQUEST_TTL`` never fires, and neither does
  one with an empty name or an unusable capture rect;
* the module never blocks Talon's serial main thread (no blocking
  command-client primitive, no in-module sleep or key press).
"""

import time

NAME_CHARS = 48
MAX_APPS = 30
REQUEST_TTL = 10.0
MAX_CAPTURE_PX = 8000


def clean_name(name):
    """Talon's display name for one app: printable, stripped, bounded."""
    text = name if isinstance(name, str) else ""
    return "".join(ch for ch in text.strip() if ch.isprintable())[:NAME_CHARS]


def build_state_doc(apps, focused=""):
    """Stable app list for apps_state.json: deduped, sorted, bounded."""
    items = apps if isinstance(apps, (list, tuple, set)) else []
    seen = {clean_name(a) for a in items}
    seen.discard("")
    return {"apps": sorted(seen)[:MAX_APPS], "focused": clean_name(focused)}


def _stale(req, now=None):
    if not isinstance(req, dict):
        return True
    ts = req.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return True
    now = time.time() if now is None else now
    return (now - ts) > REQUEST_TTL


def handle_focus_doc(req, focus, now=None):
    """Answer one focus request; ``focus`` is the Talon-side actuator.

    Refuses stale or nameless requests WITHOUT calling ``focus``, so a
    late tap can never raise the wrong app."""
    if not isinstance(req, dict):
        return {"id": None, "ok": False, "error": "bad request"}
    rid = req.get("id")
    if _stale(req, now):
        return {"id": rid, "ok": False, "error": "stale request"}
    name = clean_name(req.get("name"))
    if not name:
        return {"id": rid, "ok": False, "error": "empty app name"}
    focus(name)
    return {"id": rid, "ok": True, "focused": name}


def handle_capture_doc(req, rect_of, shoot, path, now=None):
    """Answer one capture request; ``rect_of``/``shoot`` are actuators.

    The path is fixed by the caller, never taken from the request, and
    nothing is captured unless the rect is usable."""
    if not isinstance(req, dict):
        return {"id": None, "ok": False, "error": "bad request"}
    rid = req.get("id")
    if _stale(req, now):
        return {"id": rid, "ok": False, "error": "stale request"}
    try:
        x = float(req["x"])
        y = float(req["y"])
        w = float(req["w"])
        h = float(req["h"])
    except (KeyError, TypeError, ValueError):
        return {"id": rid, "ok": False, "error": "bad capture rect"}
    if not (0 < w <= MAX_CAPTURE_PX and 0 < h <= MAX_CAPTURE_PX):
        return {"id": rid, "ok": False, "error": "capture rect out of range"}
    rect = rect_of(x, y, w, h)
    shoot(rect, path)
    return {"id": rid, "ok": True}
