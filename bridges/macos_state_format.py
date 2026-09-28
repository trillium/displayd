"""macos_state_format - pure feed-document shaping for the macOS bridge.

Single concept: turning raw macOS observations into the bounded feed
document described by the WIRE CONTRACT v1 in bridges/macos_state.py,
with PII-adjacent titles truncated and redacted before leaving the
machine. No PyObjC, no network, no subprocesses -- everything here is
importable (and testable) on any platform. Live macOS reads (poll,
app_info), transport (make_sender, fetch_mouse_command, warp_mouse),
and the CLI (main) stay in macos_state.py.
"""

import json
import os
import re
import time

TITLE_CHARS = 60
TALON_STATE = os.path.expanduser("~/.talon/user/trillium_talon/"
    "trillium/plugin/mode_indicator/mode_indicator_state.json")
BUNDLE_DENY = frozenset({"com.1password.1password", "com.apple.keychainaccess"})
REDACT_RE = re.compile(r"bank|chase|ledger|1password|secret|token|passwd",
                       re.IGNORECASE)
REDACTED = "[redacted]"


def redact(title, bundle_id=""):
    """Truncate + deny-list one window title. Never raises."""
    if bundle_id in BUNDLE_DENY:
        return REDACTED
    text = title if isinstance(title, str) else ""
    text = text.strip()[:TITLE_CHARS]
    return REDACTED if REDACT_RE.search(text) else text


def _num(value):
    """CGWindowList answers X/Y as strings, W/H as numbers; coerce."""
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def containing(x, y, displays):
    """Index of the display containing (x, y), else None."""
    for i, d in enumerate(displays):
        b = d if isinstance(d, dict) else {}
        bx, by, bw, bh = _num(b.get("x")), _num(b.get("y")), _num(b.get("w")), _num(b.get("h"))
        if None not in (bx, by, bw, bh) and bx <= x < bx + bw and by <= y < by + bh:
            return i
    return None


def window_bounds(windows, pid, ax_title=""):
    """Focused-window bounds for pid from one CG pass (layer-0 only).
    Matches the AX title, else the first named window, else the topmost
    window regardless of name (window names need Screen Recording; bounds
    and order do not). None only when pid has no windows at all -- the
    honest windowless case. Never raises."""
    fallback = top = None
    for w in windows or []:
        get = getattr(w, "get", None)  # NSDictionary is not a dict
        if get is None or get("kCGWindowLayer") != 0: continue
        try: wpid = int(get("kCGWindowOwnerPID"))
        except (TypeError, ValueError): wpid = 0
        if wpid != pid: continue
        b = get("kCGWindowBounds") or {}
        x, y, bw, bh = _num(b.get("X")), _num(b.get("Y")), _num(b.get("Width")), _num(b.get("Height"))
        if None in (x, y, bw, bh) or bw <= 0 or bh <= 0:
            continue
        bounds, name = {"x": x, "y": y, "w": bw, "h": bh}, get("kCGWindowName") or ""
        if top is None: top = bounds
        if ax_title and name == ax_title: return bounds
        if fallback is None and name: fallback = bounds
    return fallback if fallback is not None else top


def build_payload(ts=None, trusted=True, app_name="", bundle_id="", pid=0, title="", bounds=None, focus_display=None, mouse=None, mouse_display=None, displays=None, talon=None):
    """One feed document. Absent values omitted, never null."""
    payload = {"ts": ts if ts is not None else time.time(),
               "accessibility_trusted": bool(trusted)}
    focus = {"app_name": str(app_name or "unknown")}
    if bundle_id:
        focus["bundle_id"] = str(bundle_id)[:128]
    if pid:
        focus["pid"] = int(pid)
    if title:
        focus["window_title"] = title
    if bounds:
        focus["window_bounds"] = dict(bounds)
    if focus_display is not None:
        focus["display_index"] = int(focus_display)
    payload["focus"] = focus
    if mouse is not None:
        m = {"x": int(mouse[0]), "y": int(mouse[1])}
        if mouse_display is not None:
            m["display_index"] = int(mouse_display)
        payload["mouse"] = m
    if displays:
        payload["displays"] = [
            {"bounds": {"x": int(d[0]), "y": int(d[1]),
                        "w": int(d[2]), "h": int(d[3])},
             "main": bool(d[4])} for d in displays]
    if talon:
        payload["talon"] = dict(talon)
    return payload


def read_talon(path=TALON_STATE):
    """Talon mode/mic from the indicator's state file. None on any error."""
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    mic = data.get("microphone")
    mic = mic if isinstance(mic, str) else ""
    talon = {"mode": str(data.get("mode") or "other")[:16], "muted": mic == "None"}
    if mic:
        talon["microphone"] = mic[:64]
    return talon
