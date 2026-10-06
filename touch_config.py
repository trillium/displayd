"""Touch configuration surface: shipped defaults, file/env overlay, and
eager validation.

Single concept: turning a JSON file plus environment variables into the one
validated config dict the service reads. Owns the shipped defaults
(``default_config``), the merge/validate path (``load_config``,
``_check_region``) and the tap-options and confidence-feedback normalizers.
The displayd endpoint default lives with the client that uses it
(touch_client) and is re-exported here; device enumeration lives with the
device loop (touch_device).
"""

import json
import os

from touch_client import DEFAULT_ENDPOINT, endpoint_allowed
from touch_resolve import action_request

# Documented local-machine default: on lnx-server the attached panel was
# previously identified as `G2Touch Multi-Touch`. This is NOT universal --
# always confirm with --list-devices on the target host.
DEFAULT_DEVICE = "/dev/input/event8"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def default_config():
    """Conservative shipped default: two wide tap zones on a 1080p panel.

    Right third advances the playlist, left third wakes the screen. The
    middle does nothing (avoids accidental taps while reading). Tune
    width/height/calibration/regions per panel in touch.json."""
    return {
        "device": DEFAULT_DEVICE,
        "endpoint": DEFAULT_ENDPOINT,
        "width": 1920,
        "height": 1080,
        "calibration": {"x_min": 0, "x_max": 4095,
                        "y_min": 0, "y_max": 4095,
                        "swap_xy": False, "invert_x": False,
                        "invert_y": False, "rotation": 0},
        "tap_max_seconds": 0.5,
        "tap_max_pixels": 40,
        "debounce_seconds": 0.3,
        # Tap-anywhere fallback: a tap that hits no configured region
        # routes to the view-selection screen (renderers/options.py) via
        # the "options" named action above. Region hits always win, so
        # existing gestures keep working; see TOUCH.md "Tap anywhere".
        "tap_options": {"enabled": True,
                          "renderer": "options",
                          "params": {}},
        # Confidence-mode tap feedback: off by default. When enabled, every
        # resolved tap (region hit AND dead-zone miss) is POSTed best-effort
        # to displayd's feed API for the touch_confidence renderer, AFTER
        # the configured action is dispatched. Feedback failures never
        # affect action dispatch. See TOUCH.md "Touchscreen confidence mode".
        "confidence_feedback": {"enabled": False,
                                  "renderer": "touch_confidence",
                                  "input": "tap"},
        "regions": [
            {"id": "playlist-next",
             "rect": [1280, 0, 640, 1080],
             "action": {"name": "playlist_next"}},
            {"id": "screen-on",
             "rect": [0, 0, 640, 1080],
             "action": {"name": "screen_on"}},
        ],
        # Per-view regions: view_regions.<view> is live ONLY while that
        # view shows (see touch_audit.py). Plain "regions" above stay
        # global. View-specific areas sharing screen space (picker tiles
        # vs the macbook map) MUST be scoped -- global sets cannot tell
        # them apart and the wrong one wins the hit-test.
        "view_regions": {},
    }


def load_config(path=None):
    """Load JSON config file over the defaults. Env overrides (highest
    precedence): DISPLAYD_TOUCH_DEVICE, DISPLAYD_URL/DISPLAYD_ENDPOINT,
    DISPLAYD_TOUCH_WIDTH/HEIGHT. Validates regions/actions and the endpoint
    caller rule eagerly so a bad config fails before the device is opened."""
    cfg = default_config()
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            overlay = json.load(fh)
        if not isinstance(overlay, dict):
            raise ValueError("touch config must be a JSON object")

        for key, value in overlay.items():
            if key == "calibration" and isinstance(value, dict):
                cfg["calibration"].update(value)
            else:
                cfg[key] = value
    env = os.environ
    if env.get("DISPLAYD_TOUCH_DEVICE"):
        cfg["device"] = env["DISPLAYD_TOUCH_DEVICE"]
    if env.get("DISPLAYD_URL"):
        cfg["endpoint"] = env["DISPLAYD_URL"]
    if env.get("DISPLAYD_ENDPOINT"):
        cfg["endpoint"] = env["DISPLAYD_ENDPOINT"]
    for key, var in (("width", "DISPLAYD_TOUCH_WIDTH"),
                     ("height", "DISPLAYD_TOUCH_HEIGHT")):
        if env.get(var):
            cfg[key] = int(env[var])
    if env.get("DISPLAYD_TOUCH_CONFIDENCE"):
        base = cfg.get("confidence_feedback")
        cfg["confidence_feedback"] = (
            dict(base) if isinstance(base, dict) else {})
        cfg["confidence_feedback"].update(
            confidence_env_override(env["DISPLAYD_TOUCH_CONFIDENCE"]))
    if cfg["width"] <= 0 or cfg["height"] <= 0:
        raise ValueError("width/height must be positive")
    if not endpoint_allowed(cfg["endpoint"]):
        raise ValueError(
            "endpoint %r is not loopback or tailnet: touch only calls "
            "displayd on the same host or over the tailnet, never the "
            "open internet (see TOUCH.md)" % (cfg["endpoint"],))
    seen = set()
    for region in cfg.get("regions") or []:
        _check_region(region, seen, "regions")
    scoped = cfg.get("view_regions") or {}
    if not isinstance(scoped, dict):
        raise ValueError("view_regions must be an object")
    for view, entries in scoped.items():
        if (not view or not isinstance(view, str) or "/" in view):
            raise ValueError("view_regions needs plain view names")
        if not isinstance(entries, list) or not entries:
            raise ValueError("view_regions[%r] needs a non-empty list"
                             % (view,))
        for region in entries:
            _check_region(region, seen, "view_regions[%s]" % view)
    cfg["confidence_feedback"] = normalize_confidence_feedback(
        cfg.get("confidence_feedback"))
    cfg["tap_options"] = normalize_tap_options(cfg.get("tap_options"))
    return cfg


def _check_region(region, seen, where):
    """Validate one region entry; ids unique across global AND every
    scoped set (hit-test identity must be unambiguous). Fail fast."""
    rid = region.get("id") if isinstance(region, dict) else None
    if not rid or not isinstance(rid, str) or rid in seen:
        raise ValueError("%s: regions need unique string ids" % where)
    seen.add(rid)
    rect = region.get("rect")
    if (not isinstance(rect, (list, tuple)) or len(rect) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   for v in rect)):
        raise ValueError("region %r needs rect [x, y, w, h]" % (rid,))
    # Config-time shape check: coordinate actions (macbook_mouse)
    # carry no x/y yet -- the tap supplies them at dispatch -- so
    # missing coordinates are allowed here and required at dispatch.
    action_request(region.get("action") or {},
                   allow_missing_coords=True)  # fail fast on bad actions

CONFIDENCE_DEFAULTS = {"enabled": False,
                         "renderer": "touch_confidence",
                         "input": "tap"}

TAP_OPTIONS_DEFAULTS = {"enabled": True,
                          "renderer": "options",
                          "params": {}}


def normalize_tap_options(value):
    """Normalize the tap-anywhere fallback to a validated dict.

    Accepts a bool (shorthand for {"enabled": bool}) or a dict; anything
    else is a config error. The renderer must be a plain view name and
    params a plain object -- the fallback dispatches through the closed
    "options" named action, so it inherits the table's fixed-shape body
    and the loopback/tailnet caller rule. On by default: an unconsumed
    tap lands the view-selection screen from every view."""
    if value is None:
        value = {}
    if isinstance(value, bool):
        value = {"enabled": value}
    if not isinstance(value, dict):
        raise ValueError("tap_options must be a bool or an object")
    merged = dict(TAP_OPTIONS_DEFAULTS)
    merged.update(value)
    if not isinstance(merged["enabled"], bool):
        raise ValueError("tap_options.enabled must be a bool")
    if (not isinstance(merged["renderer"], str)
            or not merged["renderer"].strip()
            or "/" in merged["renderer"]):
        raise ValueError("tap_options.renderer must be a plain view name")
    merged["renderer"] = merged["renderer"].strip()
    if not isinstance(merged.get("params"), dict):
        raise ValueError("tap_options.params must be an object")
    merged["params"] = dict(merged["params"])
    return merged


def normalize_confidence_feedback(value):
    """Normalize the confidence_feedback switch to a validated dict.

    Accepts a bool (shorthand for {"enabled": bool}) or a dict; anything
    else is a config error. Missing keys fall back to CONFIDENCE_DEFAULTS.
    Off by default: absent/false means taps dispatch actions with no feed
    traffic, exactly like before confidence mode existed."""
    if value is None:
        value = {}
    if isinstance(value, bool):
        value = {"enabled": value}
    if not isinstance(value, dict):
        raise ValueError("confidence_feedback must be a bool or an object")
    merged = dict(CONFIDENCE_DEFAULTS)
    merged.update(value)
    if not isinstance(merged["enabled"], bool):
        raise ValueError("confidence_feedback.enabled must be a bool")
    for key in ("renderer", "input"):
        if (not isinstance(merged[key], str) or not merged[key].strip()
                or "/" in merged[key]):
            raise ValueError("confidence_feedback.%s must be a plain "
                             "feed name" % key)
        merged[key] = merged[key].strip()
    return merged


def confidence_env_override(text):
    """Parse DISPLAYD_TOUCH_CONFIDENCE into a confidence_feedback overlay.

    Truthy (1/true/yes/on) enables, falsy (0/false/no/off) disables;
    anything else is a config error rather than a silent default."""
    norm = str(text).strip().lower()
    if norm in ("1", "true", "yes", "on"):
        return {"enabled": True}
    if norm in ("0", "false", "no", "off"):
        return {"enabled": False}
    raise ValueError("DISPLAYD_TOUCH_CONFIDENCE must be a bool "
                     "(1/true/yes/on or 0/false/no/off), got %r" % (text,))
