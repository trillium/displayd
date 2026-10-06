"""The single reader of the closed action table: validate a named action
and build its fixed-shape displayd HTTP request.

Single concept: turning a configured action dict into ``(method, path,
body)`` or a silent denial. ``_resolve`` is the one place the table is
read; ``resolve_action`` is the silent runtime variant and
``action_request`` the strict fail-fast variant used by config validation.
"""

import logging

from touch_actions import ACTION_TABLE, COORD_ACTIONS

LOG = logging.getLogger("displayd-touch")


def _resolve(action, panel=None, allow_missing_coords=False):
    """Pure resolver: action dict -> ((method, path, body), None) on
    success, (None, reason) on denial. Both public variants below go
    through here so the table has exactly one reader.

    `panel` is an optional (width, height) pair bounding coordinate
    actions; `allow_missing_coords` is config-time only (load_config
    validates the region shape before any tap supplies coordinates)."""
    name = action.get("name") if isinstance(action, dict) else None
    spec = ACTION_TABLE.get(name)
    if spec is None:
        return None, "refusing unknown action: %r" % (name,)
    if name in ("playlist_next", "playlist_pause", "playlist_resume",
                "screen_on", "screen_off", "clear", "reload_confirm"):
        if name == "reload_confirm":
            # Fixed-shape tap confirm for the reload view only: the daemon
            # gates on the view (no reload showing -> 409 miss), so the tap
            # region needs no parameters and smuggles nothing.
            return ("POST", "/reload/confirm", {"via": "tap"}), None
        return (spec["method"], spec["path"], {}), None
    if name == "show":
        renderer = action.get("renderer")
        if not renderer or not isinstance(renderer, str):
            return None, "show action needs a renderer name"
        params = action.get("params") or {}
        if not isinstance(params, dict):
            return None, "show params must be an object"
        return ("POST", "/show",
                {"renderer": renderer, "params": params}), None
    if name == "options":
        # Tap-anywhere fallback: same endpoint as "show" but the renderer
        # defaults to the view-selection screen, so a bare
        # {"name": "options"} always lands the options view. An explicit
        # renderer override is allowed (same validation as "show") for
        # hosts that point the fallback at a branded selection screen.
        renderer = action.get("renderer", "options")
        if not renderer or not isinstance(renderer, str):
            return None, "options action needs a renderer name"
        params = action.get("params") or {}
        if not isinstance(params, dict):
            return None, "options params must be an object"
        return ("POST", "/show",
                {"renderer": renderer, "params": params}), None
    if name == "select_view":
        # View-reroute tile: {"name": "select_view", "view": "clock"}
        # posts the pinned body {"renderer": view, "params": {}} to
        # POST /show. The view must be a plain name (no "/", no blanks)
        # so the action can only ever address that one endpoint -- it can
        # never become an arbitrary-path action. Membership in the
        # daemon's advertised renderer set (GET /renderers) is enforced
        # where the set lives: show() rejects unknown names with the
        # current view undisturbed; `touch.py --check-views` cross-checks
        # a config file against the live set before it ships to the host.
        view = action.get("view")
        if not view or not isinstance(view, str):
            return None, "select_view action needs a view name"
        view = view.strip()
        if not view or "/" in view:
            return None, "select_view view must be a plain view name"
        return ("POST", "/show",
                {"renderer": view, "params": {}}), None
    if name == "macbook_mode":
        # Header control: pin GLANCE/AIM on the showing merged feature.
        # The mode rides in config (never a free path or name), so the
        # action can only ever re-pin this view's mode, never navigate.
        mode = action.get("mode")
        if not mode or not isinstance(mode, str):
            return None, "macbook_mode action needs a mode"
        mode = mode.strip().lower()
        if mode not in ("glance", "aim"):
            return None, ("macbook_mode mode must be glance or aim, "
                           "got %r" % (action.get("mode"),))
        spec = ACTION_TABLE[name]
        return (spec["method"], spec["path"], {"mode": mode}), None
    if name == "talon_tab":
        # Header stepper: page the app strip one window back/forward.
        # The page direction rides in config (+1/-1 only); the daemon
        # clamps it against the live app count, so the action aims at
        # nothing.
        direction = action.get("dir", action.get("direction"))
        if isinstance(direction, bool) or not isinstance(direction, int):
            return None, "talon_tab dir must be an integer +1 or -1"
        if abs(direction) != 1:
            return None, "talon_tab dir must be +1 or -1"
        spec = ACTION_TABLE[name]
        return (spec["method"], spec["path"],
                {"dir": direction}), None
    if name == "notify":
        title = action.get("title")
        if not title or not isinstance(title, str):
            return None, "notify action needs a title"
        body = {"title": title}
        for key in ("body", "severity", "duration", "color"):
            if key in action:
                body[key] = action[key]
        return ("POST", "/notify", body), None
    if name in COORD_ACTIONS:
        # Tap-positioned action: the region carries no x/y (stamped at
        # dispatch from the real tap); load_config validates the shape
        # with allow_missing_coords, dispatch always requires the point.
        # Out-of-range or malformed points are REFUSED, never clamped:
        # a clamp would silently focus somewhere plausible but wrong.
        # The daemon re-validates (authoritative) and additionally
        # gates on the list view showing plus a fresh feed, so a stale
        # tap can never mis-focus an app.
        spec = ACTION_TABLE[name]
        x, y = action.get("x"), action.get("y")
        if x is None or y is None:
            if allow_missing_coords:
                return (spec["method"], spec["path"], {}), None
            return None, ("%s action needs tap coordinates" % name)
        for value in (x, y):
            if isinstance(value, bool) or not isinstance(value, int):
                return None, ("%s coordinates must be integers, "
                               "got %r,%r" % (name, x, y))
        if panel is not None:
            width, height = panel
            if not (0 <= x < width and 0 <= y < height):
                return None, ("%s coordinates off-panel: "
                               "%r,%r for %dx%d" % (name, x, y,
                                                     width, height))
        elif x < 0 or y < 0:
            return None, ("%s coordinates must be non-negative, "
                           "got %r,%r" % (name, x, y))
        return (spec["method"], spec["path"], {"x": x, "y": y}), None
    if name == "feedback":
        view = action.get("view")
        if not view or not isinstance(view, str):
            return None, "feedback action needs a view name"
        rating = action.get("rating")
        if (isinstance(rating, bool) or not isinstance(rating, int)
                or not 1 <= rating <= 5):
            return None, "feedback rating must be an integer 1-5"
        body = {"view": view, "rating": rating, "agent": "touch"}
        # Categories only: notes/params stay out (named residue above),
        # so a tap records a fixed-shape rating and nothing else.
        if "categories" in action:
            categories = action["categories"]
            if (not isinstance(categories, (list, tuple))
                    or any(not c or not isinstance(c, str)
                           for c in categories)):
                return None, "feedback categories must be a list of names"
            body["categories"] = list(categories)
        return ("POST", "/feedback", body), None
    return None, "refusing unknown action: %r" % (name,)  # pragma: no cover


def resolve_action(action, panel=None, allow_missing_coords=False):
    """Silent variant (index.ts shape): (method, path, body), or None
    when the action is denied. The denial is logged and nothing else
    happens -- no HTTP, no exception in the service loop."""
    resolved, reason = _resolve(action, panel=panel,
                                allow_missing_coords=allow_missing_coords)
    if resolved is None:
        name = action.get("name") if isinstance(action, dict) else None
        LOG.warning("touch action denied (closed allowlist): %s", reason)
        return None
    return resolved


def action_request(action, panel=None, allow_missing_coords=False):
    """Strict variant: translate a validated action dict into
    (method, path, body).

    Raises ValueError for unknown action names (closed allowlist) or
    malformed payloads. Used for fail-fast config validation in
    load_config(); runtime dispatch prefers resolve_action() above.
    Returned bodies are fixed-shape; free-form renderer params are
    allowed only for the `show` target renderer named in config.
    """
    resolved, reason = _resolve(action, panel=panel,
                                allow_missing_coords=allow_missing_coords)
    if resolved is None:
        raise ValueError(reason)
    return resolved
