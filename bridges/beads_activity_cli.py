"""Command-line entry point for the beads-activity bridge.

Single concept: parse argv/env into an ActivityBridge and run it until
killed. Invoked through the ``__main__`` guard of ``beads_activity.py``
(which the LaunchAgent plist executes directly); kept in its own module
so each file stays within the project's line budget.
"""

import argparse
import logging
import os

from beads_activity import ActivityBridge


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Beads-bridge live activity -> displayd bridge")
    ap.add_argument("--bridge",
                    default=os.environ.get("BEADS_BRIDGE_URL",
                                           "http://127.0.0.1:3737"),
                    help="beads-bridge base URL (default: %(default)s)")
    ap.add_argument("--displayd",
                    default=os.environ.get("DISPLAYD_BASE",
                                           "http://100.81.88.113:8980"),
                    help="displayd base URL (default: %(default)s)")
    ap.add_argument("--interval", type=float,
                    default=float(os.environ.get("ACTIVITY_INTERVAL", "3.0")),
                    help="poll seconds, 1..120 (default: %(default)s)")
    ap.add_argument("--limit", type=int,
                    default=int(os.environ.get("ACTIVITY_LIMIT", "50")),
                    help="events per poll, 1..200 (default: %(default)s)")
    ap.add_argument("--backfill", type=int,
                    default=int(os.environ.get("ACTIVITY_BACKFILL", "8")),
                    help="newest events to forward on first poll, 0..50 "
                         "(default: %(default)s)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        bridge = ActivityBridge(args.bridge, args.displayd,
                                interval=args.interval, limit=args.limit,
                                backfill=args.backfill)
    except ValueError as err:
        ap.error(str(err))
        return 2
    bridge.run_forever()
    return 0
