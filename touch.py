"""Touch input for displayd: evdev touchscreen -> displayd HTTP actions.

Standalone, stdlib-only companion to the output-only ``displayd`` daemon.
It reads a Linux input device (``/dev/input/event*``), decodes evdev
pointer/multitouch events, normalizes them into display coordinates, hit-tests
them against a small configured region list, and invokes existing safe
displayd HTTP actions for taps.

Nothing here touches the renderer core or the framebuffer path: the only
coupling to displayd is its public HTTP API (the same endpoints a remote
operator would call). The trust boundary is therefore the displayd HTTP API
itself, which is unauthenticated by design -- see "Transport and trust
boundary" in TOUCH.md.

This module is the command-line entry point and the stable import surface.
The implementation is split into single-concept modules next door:
:mod:`touch_events` (evdev wire format), :mod:`touch_actions` (the closed
named-action allowlist), :mod:`touch_resolve` (the table's single reader),
:mod:`touch_regions` (display geometry + hit testing), :mod:`touch_client`
(the displayd HTTP client and caller rule), :mod:`touch_config` (defaults +
load/validate), :mod:`touch_taps` (tap detection) and :mod:`touch_service`
(the service class) with its concept mixins in :mod:`touch_heartbeat`,
:mod:`touch_scope`, :mod:`touch_feedback`, :mod:`touch_dispatch` and
:mod:`touch_device`. Their public names are re-exported below, so ``import
touch`` means the same module surface it always did.

Run foreground (smoke test)::

    python3 touch.py --list-devices
    python3 touch.py --device /dev/input/event8 --width 1920 --height 1080 --dry-run
    python3 touch.py --config touch.json

See TOUCH.md for device discovery, permissions, calibration, supervision,
and rollback. See tests/test_touch.py for the deterministic fixture suite
(no touchscreen hardware required).
"""

import argparse
import json
import logging
import signal
import sys
import time  # noqa: F401  (kept: tests patch touch.time.monotonic)

import touch_audit  # noqa: F401  (kept: touch.touch_audit module attribute)

from touch_actions import ACTION_TABLE, ALLOWED_ACTIONS, COORD_ACTIONS
from touch_client import (DEFAULT_ENDPOINT, TAP_DISMISS_PATH, TAILNET_CGNAT,
                          DisplaydClient, check_views, endpoint_allowed,
                          fetch_renderers, post_announce)
from touch_config import (CONFIDENCE_DEFAULTS, DEFAULT_DEVICE,
                          TAP_OPTIONS_DEFAULTS, confidence_env_override,
                          default_config, load_config,
                          normalize_confidence_feedback, normalize_tap_options)
from touch_events import (ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_SLOT,
                          ABS_MT_TOUCH_MAJOR, ABS_MT_TRACKING_ID, ABS_X, ABS_Y,
                          BTN_TOUCH, EV_ABS, EV_KEY, EV_SYN, EVENT_FORMAT,
                          EVENT_SIZE, READ_POLL_SECONDS, SYN_REPORT,
                          EvdevParser, TouchEvent, pack_event, parse_event)
from touch_heartbeat import ANNOUNCE_INTERVAL_SECONDS
from touch_regions import hit_test, normalize
from touch_resolve import action_request, resolve_action
from touch_service import TouchService
from touch_taps import TapDetector

LOG = logging.getLogger("displayd-touch")

__all__ = [
    # re-exported implementation surface (see the module docstring)
    "ABS_MT_POSITION_X", "ABS_MT_POSITION_Y", "ABS_MT_SLOT",
    "ABS_MT_TOUCH_MAJOR", "ABS_MT_TRACKING_ID", "ABS_X", "ABS_Y",
    "ACTION_TABLE", "ALLOWED_ACTIONS", "ANNOUNCE_INTERVAL_SECONDS",
    "BTN_TOUCH", "CONFIDENCE_DEFAULTS", "COORD_ACTIONS", "DEFAULT_DEVICE",
    "DEFAULT_ENDPOINT", "DisplaydClient", "EVENT_FORMAT", "EVENT_SIZE",
    "EV_ABS", "EV_KEY", "EV_SYN", "EvdevParser", "READ_POLL_SECONDS",
    "SYN_REPORT", "TAILNET_CGNAT", "TAP_DISMISS_PATH", "TAP_OPTIONS_DEFAULTS",
    "TapDetector", "TouchEvent", "TouchService", "action_request",
    "check_views", "confidence_env_override", "default_config",
    "endpoint_allowed", "fetch_renderers", "hit_test", "load_config",
    "normalize", "normalize_confidence_feedback", "normalize_tap_options",
    "pack_event", "parse_event", "post_announce", "resolve_action", "time",
    "touch_audit",
]


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="displayd touch input: evdev touchscreen -> "
                    "displayd HTTP actions")
    parser.add_argument("--config", default=None,
                        help="JSON config file (default: built-ins + env)")
    parser.add_argument("--device", default=None,
                        help="input device (default: %s; always confirm "
                             "with --list-devices)" % DEFAULT_DEVICE)
    parser.add_argument("--endpoint", default=None,
                        help="displayd base URL "
                             "(default: http://127.0.0.1:8980)")
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="decode + hit-test, log actions, send no HTTP")
    parser.add_argument("--confidence-feedback", action="store_true",
                        help="publish resolved taps to the touch_confidence "
                             "feed (same as DISPLAYD_TOUCH_CONFIDENCE=1)")
    parser.add_argument("--list-devices", action="store_true",
                        help="list /dev/input/event* with names and exit")
    parser.add_argument("--check-views", action="store_true",
                        help="cross-check configured select_view tiles + "
                             "tap_options renderer against GET /renderers "
                             "and exit 0/1 (no device needed)")
    parser.add_argument("--announce", action="store_true",
                        help="POST the config file's region set to "
                             "/touch/announce and exit (no device needed; "
                             "use after verifying the running service "
                             "matches the file, e.g. post displayd "
                             "restart)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.list_devices:
        for dev in list_input_devices():
            print("%s\t%s" % (dev["path"], dev["name"] or "(unknown)"))
        return 0
    cfg = load_config(args.config)
    if args.device:
        cfg["device"] = args.device
    if args.endpoint:
        cfg["endpoint"] = args.endpoint
    if args.width:
        cfg["width"] = args.width
    if args.height:
        cfg["height"] = args.height
    # CLI overrides bypass load_config's fail-fast, so re-apply the
    # caller rule here before the device is opened.
    if not endpoint_allowed(cfg["endpoint"]):
        parser.error("endpoint %r is not loopback or tailnet: touch "
                     "only calls displayd on the same host or over the "
                     "tailnet" % (cfg["endpoint"],))
    if args.confidence_feedback:
        cfg["confidence_feedback"] = normalize_confidence_feedback(
            dict(cfg.get("confidence_feedback") or {}, enabled=True))
    if args.check_views:
        try:
            report = check_views(cfg)
        except RuntimeError as exc:
            print("check-views: %s" % exc)
            return 2
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    if args.announce:
        try:
            print(json.dumps(post_announce(cfg), indent=2,
                             sort_keys=True))
            return 0
        except RuntimeError as exc:
            print("announce: %s" % exc)
            return 2
    if not args.dry_run:
        # Heartbeat first: displayd's /touch/check compares the DRAWN UI
        # against this announced set, not the file. Best-effort -- taps
        # must serve even when displayd is unreachable; the check then
        # reports blind until an announce lands. run() renews it
        # in-process every ANNOUNCE_INTERVAL_SECONDS, far inside the
        # daemon's TOUCH_HEARTBEAT_MAX_AGE, so the gate never ages out
        # between deploys.
        try:
            ack = post_announce(cfg)
            LOG.info("touch announce: %d regions live (%s)",
                     ack.get("regions", 0), ack.get("regions_sha"))
        except RuntimeError as exc:
            LOG.warning("touch announce failed (best-effort, taps still "
                        "serve): %s", exc)
    service = TouchService(cfg)
    signal.signal(signal.SIGINT,
                  lambda *_a: service.request_stop())
    signal.signal(signal.SIGTERM,
                  lambda *_a: service.request_stop())
    service.run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
