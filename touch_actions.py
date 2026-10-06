"""The closed named-action allowlist: which panel-state-changing daemon
endpoints a tap may ever drive.

Single concept: the allowlist itself, classified by handler effect (the
parlay guard doctrine below), plus its name list and the coordinate-action
set. The table's single reader -- the code that validates a named action
and builds its fixed-shape HTTP request -- lives in touch_resolve.py.
"""


# ---------------------------------------------------------------------------
# Named-action allowlist + caller rule (parlay guard shape)
# ---------------------------------------------------------------------------
# This section mirrors trillium/parlay's chat guard,
# packages/server/src/guard/paths.ts, origin.ts, index.ts:
#
#   paths.ts  owns WHICH routes are guarded -- a closed set classified by
#             handler effect, with the accepted residue named there.
#   origin.ts owns WHO may call them (loopback / private-LAN / allow-list).
#   index.ts  applies the policy with silent denies (403/415 carrying no
#             CORS headers, so a refused caller learns nothing back).
#
# Mapped onto touch:
#   ACTION_TABLE      (this module)  <-> paths.ts:  WHICH named actions may
#                                        ever run, classified by handler
#                                        effect (which daemon endpoint the
#                                        tap drives + the fixed body shape),
#                                        never by the action's name. Closed
#                                        by default; unknown names are
#                                        denied silently (resolve_action()
#                                        returns None: logged, no HTTP, no
#                                        exception in the service loop).
#   endpoint_allowed() (touch_client.py) <-> origin.ts: WHO/WHERE may be called
#                                        -- loopback or tailnet only, never
#                                        the open internet. Deliberately
#                                        stricter than parlay (which also
#                                        admits private-LAN for the phone
#                                        panel): touch has no LAN caller.
#   DisplaydClient.dispatch() (touch_client.py) <-> index.ts: applies both halves at
#                                        dispatch time; denials return an
#                                        error summary, never an exception
#                                        and never a byte on the wire.
#
# Parlay's classification rule, quoted (paths.ts, guarded-route-set
# comment): the guarded set is "the routes that write server state, drive
# a device, or hand out an identifier the rest of the surface can then be
# aimed with. Within that surface, membership is decided by what the
# handler DOES, REGARDLESS OF HTTP METHOD." The touch analogue: the table
# is the PANEL-STATE-CHANGING surface -- every entry POSTs one displayd
# endpoint that changes what the panel shows or records. Membership is
# decided by that handler effect, never by the action name's spelling.
#
# Named residue (deliberately outside the table, not a queue that grows
# one entry per request): no generic "POST any path" action, no shell-out
# action, no free-text feedback notes/params passthrough, no MagicDNS /
# LAN / public endpoint. Each is the same class of defect this table
# exists to prevent (a bad config becoming command execution or an
# internet-reachable caller), so each stays out until a named action with
# a fixed body shape justifies it -- see TOUCH.md "how to add a named
# action".
#
# ACTION_TABLE maps action name -> handler effect. "method"/"path" are
# the daemon endpoint the tap drives; "effect" states the classification
# (what changes on the panel) for the next reader. Actions needing
# config-supplied parameters declare them under "params" and validate
# them in _resolve(); anything else in the action dict is ignored.
ACTION_TABLE = {
    "playlist_next": {
        "effect": "advance playlist rotation",
        "method": "POST", "path": "/playlist/next",
    },
    "playlist_pause": {
        "effect": "hold playlist rotation",
        "method": "POST", "path": "/playlist/pause",
    },
    "playlist_resume": {
        "effect": "resume playlist rotation",
        "method": "POST", "path": "/playlist/resume",
    },
    "screen_on": {
        "effect": "drive panel backlight on",
        "method": "POST", "path": "/screen/on",
    },
    "screen_off": {
        "effect": "drive panel backlight off",
        "method": "POST", "path": "/screen/off",
    },
    "clear": {
        "effect": "blank the panel",
        "method": "POST", "path": "/clear",
    },
    "show": {
        "effect": "replace the shown view (renderer named in config)",
        "method": "POST", "path": "/show",
        "params": ("renderer", "params"),
    },
    "options": {
        "effect": "route an unconsumed tap to the view-selection screen",
        "method": "POST", "path": "/show",
        "params": ("renderer", "params"),
    },
    "select_view": {
        "effect": "reroute the displayed view to the named selection",
        "method": "POST", "path": "/show",
        "params": ("view",),
    },
    "notify": {
        "effect": "interrupt the panel with a transient notice",
        "method": "POST", "path": "/notify",
        "params": ("title", "body", "severity", "duration",
                   "color"),
    },
    "feedback": {
        "effect": "record a fixed-shape tap-to-rate feedback rating",
        "method": "POST", "path": "/feedback",
        "params": ("view", "rating", "categories"),
    },
    "reload_confirm": {
        "effect": "confirm the showing reload view via tap (view-gated: "
                    "no-op unless the reload QR view is showing)",
        "method": "POST", "path": "/reload/confirm",
    },
    "macbook_mouse": {
        "effect": "move the MacBook cursor to the tapped map point "
                    "(view+mode-gated: refused unless the macbook view "
                    "is showing in GLANCE mode)",
        "method": "POST", "path": "/macbook/mouse",
        "params": ("x", "y"),
    },
    "talon_focus": {
        "effect": "focus the tapped header app chip (view+mode-gated: "
                    "refused unless the merged macbook view is showing "
                    "in GLANCE mode)",
        "method": "POST", "path": "/talon/focus",
        "params": ("x", "y"),
    },
    "macbook_mode": {
        "effect": "pin the merged macbook view to GLANCE or AIM "
                    "(view-gated: refused unless the macbook view is "
                    "showing; keeps the tab highlight)",
        "method": "POST", "path": "/macbook/mode",
        "params": ("mode",),
    },
    "talon_tab": {
        "effect": "page the header app strip one window back/forward "
                    "(view+mode-gated: refused unless the macbook view "
                    "is showing in GLANCE mode with a fresh apps feed; "
                    "refused at the first/last page)",
        "method": "POST", "path": "/talon/tab",
        "params": ("dir",),
    },
    "macbook_click": {
        "effect": "click the reviewed point on the fullscreen image "
                    "(view+mode-gated: refused unless the macbook view "
                    "is showing in AIM mode, plus fresh-capture and "
                    "cursor-still gates)",
        "method": "POST", "path": "/macbook/click",
        "params": ("x", "y"),
    },
}

# Actions positioned by the tap itself: the region names the action, the
# tap point positions it. Coordinates are stamped at dispatch (never
# stored in config) and validated twice -- here and daemon-side, where
# the point maps to a feed-listed app (talon_focus) or a Quartz point
# (macbook_mouse, macbook_click). All three share the _resolve branch.
COORD_ACTIONS = ("talon_focus", "macbook_mouse", "macbook_click")

# Backwards-compatible name list (was the whole allowlist before the
# guard-shaped table above). New code should read ACTION_TABLE.
ALLOWED_ACTIONS = tuple(ACTION_TABLE)
