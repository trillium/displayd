#!/usr/bin/env python3
"""displayd - a tiny content-agnostic display daemon.

Owns the physical screen of a headless Linux box and exposes it through a
small JSON API.  All content comes from renderer plugins dropped into
renderers/ -- the core knows nothing about any particular one.

API
  GET  /                             web control page (this panel)
  GET  /health                       liveness
  GET  /version                      application semver (APP_VERSION)
  GET  /state                        what is showing + screen power
  GET  /renderers                    available renderers and their params
  GET  /snapshot                     PNG of the last presented frame
  POST /show    {"renderer":"name","params":{...}}
  POST /layout  {"regions":[{"name":...,"renderer":...,"params"?,
               "height"?/"width"?/"rect"?/"row"?/"col"?...}]} static regions
  GET  /layout                       current layout (null when inactive)
  DELETE /layout                     clear the layout (blank screen)
  POST /feed/<renderer>/<input>  push a validated payload into a view
  POST /notify  {"title":...,"body"?,"severity"?,"duration"?} transient notice
  POST /reload  {"sha":..., "highlights"?} reload confirmation
               (RELOADED + SHA + QR, plus an optional bounded
               commit-message summary drawn as text only, never in
               the QR), stays until confirmed: a scan of the relay QR
               or a tap returns early (POST /reload/confirm,
               POST /touch/tap)
  GET  /r/<token>  one-time scan relay: 302 to the commit page + confirm
  POST /reload/confirm  {"via"?} tap/scan confirm path for the reload
               view only (409 when none showing)
  POST /touch/tap  dismiss an active reload transient (tap-to-return),
               no-op for anything else
  POST /touch/resolve  {x,y} or {x_norm,y_norm}: read-only tap
               resolution against the CURRENT UI (region + semantic
               action, view/mode-gated like the touch path); dispatches
               nothing, changes no state
  GET  /policy  autonomous-behaviour config + activity clock
  POST /policy  {"idle":{...},"chat_attention":{...},"notifications":{...}}
  POST /clear                        blank the screen to black
  POST /screen  {"power":"on"|"off"} also /screen/on and /screen/off

The API has no authentication, so it listens on 127.0.0.1 by default.  Bind
wider only deliberately -- see --bind / --port (or DISPLAYD_BIND / DISPLAYD_PORT).
The daemon itself is assembled here. ``DisplayDaemon`` is composed from the
single-concept mixins next door -- construction (daemon_core), the single-view
lifecycle (daemon_view), transients (daemon_transient), the reload view and its
QR relay (daemon_reload, daemon_relay), feedback (daemon_feedback), feeds
(daemon_feeds), the touch audit gate and read-only tap resolution
(daemon_touch_audit, daemon_touch_resolve), the Mac-side command queues
(daemon_mouse, daemon_focus, daemon_macbook_mode, daemon_click), the command
long-poll (daemon_commands), static-region layout (daemon_layout), screen
power and the policy actuators (daemon_power), and read-only introspection
(daemon_status) -- and the HTTP surface from daemon_http.py,
daemon_http_get.py and daemon_http_post.py. The framebuffer backends,
presentation, renderer registry, schema validation, layout parsing and feed
cache live in their own modules.

This module is the composition root. It owns the mutable runtime globals the
moved code reads back through the bound names -- ``DAEMON``,
``API_TOKEN`` and the ``Framebuffer`` test hook -- so the documented
monkeypatch contracts (patching ``displayd.DAEMON`` / ``displayd.API_TOKEN`` /
``displayd.Framebuffer``) keep working, plus the entry point and the
re-exports that keep ``import displayd`` a stable surface.
"""

import argparse
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from control_page import CONTROL_PAGE
from daemon_click import ClickMixin
from daemon_commands import CommandsMixin
from daemon_config import APP_VERSION, BIND, PORT
from daemon_core import DisplayCoreMixin
from daemon_feedback import FeedbackMixin
from daemon_feeds import FeedsMixin
from daemon_focus import FocusMixin
from daemon_http import HandlerBaseMixin
from daemon_http_get import GETRoutesMixin
from daemon_http_post import POSTRoutesMixin
from daemon_layout import LayoutMixin
from daemon_macbook_mode import MacbookModeMixin
from daemon_mouse import MouseMixin
from daemon_power import POLICY_FILE, SLEEP_VIEW, PowerMixin
from daemon_reload import ReloadMixin
from daemon_relay import RelayMixin
from daemon_status import StatusMixin
from daemon_touch_audit import TOUCH_JSON_PATH, TouchAuditMixin, touch_audit
from daemon_touch_resolve import TouchResolveMixin, _resolve_coords
from daemon_transient import TransientMixin
from daemon_view import ViewLifecycleMixin
from deploy_reload import (DEPLOY_STAMP_FILE, RELOAD_COMMIT_URL_PREFIX,
                           RELOAD_DEFAULT_DURATION, RELOAD_DURATION_MAX,
                           RELOAD_DURATION_MIN, RELOAD_RELAY_PATH,
                           RELOAD_SHA_RE, RELOAD_TOKEN_RE, TAILNET_CGNAT,
                           _relay_bind_host, _relay_port, deploy_stamp_path,
                           read_deploy_stamp, relay_base_url)
from feed_store import FeedStore
from framebuffer import (BACKLIGHT_GLOB, FB, FB_SYS, KD_GRAPHICS, KD_TEXT,
                         KDSETMODE, VT, Framebuffer, _read)
from framebuffer_headless import HeadlessFramebuffer
from layout import (MAX_LAYOUT_REGIONS, _assign_grid, _assign_rect,
                    _assign_stack, _grid_rect, _parse_dim, parse_layout)
from renderer_registry import (RENDERER_DIR, _load_shared_helper,
                               load_renderers, macbook_layout_module,
                               macbook_map_module, system_buttons_module,
                               talon_apps_module)
from schema import INPUT_TYPES, _type_ok, validate_params, validate_value
from screen import RegionScreen, Screen

# Optional shared secret for the HTTP API. When set, every request (except
# the unauthenticated health probes) must carry
#   Authorization: Bearer <token>
# Unset means the API stays open -- the historical default -- and the
# loopback binding remains the only protection. Kept in this module (not in
# daemon_config) because tests patch displayd.API_TOKEN directly; Handler
# reads it back through the api_token property below.
API_TOKEN = os.environ.get("DISPLAYD_API_TOKEN", "").encode()

__all__ = [
    "API_TOKEN", "APP_VERSION", "BACKLIGHT_GLOB", "BIND", "CONTROL_PAGE",
    "DAEMON", "DEPLOY_STAMP_FILE", "DisplayDaemon", "FB", "FB_SYS",
    "FeedStore", "Framebuffer", "Handler", "HeadlessFramebuffer",
    "INPUT_TYPES", "KD_GRAPHICS", "KD_TEXT", "KDSETMODE",
    "MAX_LAYOUT_REGIONS", "POLICY_FILE", "PORT", "RELOAD_COMMIT_URL_PREFIX",
    "RELOAD_DEFAULT_DURATION", "RELOAD_DURATION_MAX", "RELOAD_DURATION_MIN",
    "RELOAD_RELAY_PATH", "RELOAD_SHA_RE", "RELOAD_TOKEN_RE", "RENDERER_DIR",
    "RegionScreen", "SLEEP_VIEW", "Screen", "TAILNET_CGNAT", "TOUCH_JSON_PATH",
    "VT", "_assign_grid", "_assign_rect", "_assign_stack", "_grid_rect",
    "_load_shared_helper", "_parse_dim", "_read", "_relay_bind_host",
    "_relay_port", "_resolve_coords", "_type_ok", "deploy_stamp_path",
    "load_renderers", "macbook_layout_module",
    "macbook_map_module", "main", "parse_layout", "read_deploy_stamp",
    "relay_base_url", "system_buttons_module", "talon_apps_module",
    "touch_audit", "validate_params", "validate_value",
]


class DisplayDaemon(DisplayCoreMixin, ViewLifecycleMixin, TransientMixin,
                    ReloadMixin, RelayMixin, FeedbackMixin, FeedsMixin,
                    TouchAuditMixin, TouchResolveMixin, MouseMixin, FocusMixin,
                    MacbookModeMixin, ClickMixin, CommandsMixin, LayoutMixin,
                    PowerMixin, StatusMixin):
    """The display daemon: state, render loop, feeds, and policy wiring."""

    def _framebuffer(self):
        """Construct the framebuffer backend from THIS module's namespace.

        The lookup is deliberately late and local: ``displayd.Framebuffer`` is
        a documented test hook (tests swap in a recording fake before
        constructing the daemon), so the composition root -- not the
        construction mixin -- owns the binding."""
        if os.environ.get("DISPLAYD_FAKE_FB") == "1":
            return HeadlessFramebuffer()
        return Framebuffer()


class Handler(GETRoutesMixin, POSTRoutesMixin, HandlerBaseMixin,
              BaseHTTPRequestHandler):
    """The HTTP handler: routes from daemon_http*.py, runtime globals here.

    ``daemon`` and ``api_token`` are properties rather than module-level
    lookups inside the route mixins so that patching ``displayd.DAEMON`` /
    ``displayd.API_TOKEN`` (both documented test hooks) still decides what a
    live request sees."""

    @property
    def daemon(self):
        return DAEMON

    @property
    def api_token(self):
        return API_TOKEN


DAEMON = None


def main():
    global DAEMON
    parser = argparse.ArgumentParser(description="displayd - API-driven display server")
    parser.add_argument("--bind", default=BIND,
                        help="address to listen on (default: %(default)s, loopback only)")
    parser.add_argument("--port", type=int, default=PORT,
                        help="TCP port to listen on (default: %(default)s)")
    args = parser.parse_args()
    DAEMON = DisplayDaemon()
    DAEMON.clear()
    # Boot-time clear is housekeeping, not a manual choice: let a persisted
    # enabled playlist resume rotating (it restores the first view itself).
    DAEMON.playlist.boot()
    DAEMON.start_watchdog()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print("displayd v%s listening on %s:%d with %d renderer(s)"
          % (APP_VERSION, args.bind, args.port, len(DAEMON.renderers)))
    try:
        server.serve_forever()
    finally:
        DAEMON.fb.release_console()


if __name__ == "__main__":
    main()
