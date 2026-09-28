"""OBS -> displayd stream bridge CLI (argparse entry point only).

Single concept: parse flags/env into an ``ObsPoller`` and run it forever.
Reached via a lazy import in ``obs_poll.py``'s ``__main__`` guard so the
direct-script execution path (``./bridges/obs_poll.py``) is unchanged.

Stdlib only, no credentials in code or logs: the password travels via the
``OBS_PASSWORD`` environment variable only (there is deliberately no
``--password`` flag, so it never leaks through ``ps``), and no log line
ever includes it. Exits non-zero only on configuration errors; a dropped
socket or an unreachable displayd is a reconnect/retry, never a death.

Live checklist against real OBS (run once, then leave the bridge running):
  1. ``OBS_SOURCE='<name>' OBS_PASSWORD='<pw>' ./bridges/obs_poll.py --verbose``
     shows ``connected`` + ``streaming`` and the panel leaves its
     "waiting for stream" idle screen for the source image.
  2. Wrong password shows exactly one ``auth-failed`` line and retries
     with backoff (fix the env var, it recovers on its own).
  3. Kill OBS: exactly one ``obs-unreachable`` line; restart OBS and the
     bridge reconnects without a restart.
  4. Stop displayd: exactly one ``displayd-unreachable`` line, frames drop,
     panel keeps its last-good frame; restart displayd and pushes resume.
  5. ``POST /show {"renderer": "stream"}`` on the panel, then confirm the
     measured fps on screen matches the configured ``--fps``.
"""

import argparse
import logging
import os

from obs_poll import DEFAULT_FPS, IMAGE_WIDTH, ObsPoller


def main(argv=None):
    ap = argparse.ArgumentParser(description="OBS -> displayd stream bridge")
    ap.add_argument("--obs-host",
                    default=os.environ.get("OBS_HOST", "100.74.138.74"),
                    help="OBS host (default: %(default)s)")
    ap.add_argument("--obs-port", type=int,
                    default=int(os.environ.get("OBS_PORT", "4455")),
                    help="obs-websocket v5 port (default: %(default)s)")
    ap.add_argument("--source", default=os.environ.get("OBS_SOURCE", ""),
                    help="OBS source name to screenshot (or OBS_SOURCE)")
    ap.add_argument("--fps", type=float,
                    default=float(os.environ.get("OBS_FPS", str(DEFAULT_FPS))),
                    help="poll rate, clamped to 0.5..5 (default: %(default)s)")
    ap.add_argument("--displayd",
                    default=os.environ.get("DISPLAYD_BASE",
                                           "http://100.81.88.113:8980"),
                    help="displayd base URL (default: %(default)s)")
    ap.add_argument("--width", type=int, default=IMAGE_WIDTH,
                    help="screenshot width cap (default: %(default)s)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # NOTE: the password comes from OBS_PASSWORD only -- no CLI flag, so it
    # never appears in ps output, and it is never logged anywhere above.
    if not args.source:
        ap.error("source name is required (--source or OBS_SOURCE)")
        return 2
    if not args.displayd:
        ap.error("displayd base URL is required")
        return 2
    ObsPoller(args.obs_host, args.obs_port, os.environ.get("OBS_PASSWORD", ""),
              args.source, args.fps, args.displayd,
              image_width=args.width).run_forever()
    return 0
