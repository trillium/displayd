#!/usr/bin/env python3
"""Mac-side review bridge: loads review beads onto the panel and runs the
decisions tapped there against the review store.

  python3 tools/review_bridge.py load <bead-id>    show that bead on the panel
  python3 tools/review_bridge.py serve             execute confirmed decisions

Run it on the MacBook (the ``review`` store, Dolt on 127.0.0.1:3307, lives
there; the panel host never touches it). Stdlib only. The panel names a bead
id and an option id and nothing else: ``serve`` re-reads the bead from the
store, re-derives its options with the same parser the panel used
(review_decision.py), checks the bead is still open and flagged ``human`` and
the option still exists, and only then runs ``review human respond|dismiss``
as an argv list (no shell). A refusal is reported back to the panel, never
retried. Environment: ``DISPLAYD_URL`` (default http://100.81.88.113:8980),
``REVIEW_BIN`` (default ``review``).
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

import review_decision as rd  # noqa: E402

STREAM = "review-decisions"
DEFAULT_URL = "http://100.81.88.113:8980"
STORE_TIMEOUT, HTTP_TIMEOUT, LONG_POLL = 30, 40, 20


def http(method, url, body=None, timeout=HTTP_TIMEOUT):
    """``(status, json)``; a refusal is data, an unreachable panel raises OSError."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode() or "{}")


def run_store(argv, run=subprocess.run):
    """``(ok, output)`` of one store command; never raises."""
    try:
        done = run(argv, capture_output=True, text=True, timeout=STORE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as err:
        return False, "store command failed: %s" % err
    return done.returncode == 0, (done.stdout if done.returncode == 0 else done.stderr).strip()


def fetch(bead_id, binary, run=subprocess.run):
    """The live decision for ``bead_id`` (``ValueError`` explains why not)."""
    if not rd.ID_RE.match(str(bead_id)):
        raise ValueError("not a review bead id: %r" % (bead_id,))
    ok, out = run_store([binary, "show", bead_id, "--json"], run)
    if not ok:
        raise ValueError("review show failed: %s" % out[-200:])
    try:
        return json.loads(out)
    except ValueError:
        raise ValueError("review show gave no JSON")


def execute(event, binary="review", run=subprocess.run):
    """Run one confirmed decision event; ``(ok, message)`` for the panel."""
    try:
        bead_id, option_id, action = event["bead"], str(event["option"]), event["action"]
        decision = rd.parse_bead(fetch(bead_id, binary, run))
    except (KeyError, TypeError, ValueError) as err:
        return False, "refused: %s" % err
    if not decision["actionable"]:
        return False, "refused: %s" % decision["reason"]
    option = next((o for o in decision["options"] if o["id"] == option_id), None)
    if option is None or option["action"] != action:
        return False, "refused: option %s is no longer on %s" % (option_id, bead_id)
    if action == "dismiss":
        argv = [binary, "human", "dismiss", bead_id]
    else:  # a leading dash would read as a flag
        text = option["value"] if not option["value"].startswith("-") \
            else "Answer: " + option["value"]
        argv = [binary, "human", "respond", bead_id, text]
    ok, out = run_store(argv, run)
    return ok, ("%s %s" % (action, bead_id)) if ok else "store refused: %s" % out[-200:]


def load(base, bead_id, binary, run=subprocess.run):
    beads = fetch(bead_id, binary, run)
    code, out = http("POST", base + "/review/load",
                     {"bead": beads[0] if isinstance(beads, list) and beads else beads})
    if code != 200:
        raise ValueError("panel refused the bead (%d): %s" % (code, out.get("error", out)))
    return out


def _report(base, event, ok, message):
    code, out = http("POST", base + "/review/result",
                     {"nonce": event.get("nonce"), "ok": ok, "message": message})
    if code != 200:  # e.g. the panel moved on: nothing more to do
        print("review-bridge: result not accepted (%d): %s" % (code, out), file=sys.stderr)


def _pending(base):
    """The decision the panel is still waiting on, if any."""
    code, state = http("GET", base + "/review")
    return state.get("nonce") if code == 200 and state.get("state") == "sending" else None


def serve(base, binary, run=subprocess.run, once=False):
    """Execute decisions as they are confirmed. On start, the ring's backlog is
    skipped except the one decision the panel is still awaiting (if any), so a
    restart never replays old taps."""
    since, owed = None, None
    while True:
        try:
            if since is None:
                code, _stream = http("GET", base + "/event-streams/" + STREAM)
                if code != 200:
                    raise OSError("stream %s not declared yet" % STREAM)
                since, owed = 0, _pending(base) or ""
            code, out = http("GET", "%s/event-streams/%s/events?since=%s&wait=%d"
                             % (base, STREAM, since, LONG_POLL))
            if code != 200:
                raise OSError("events read gave %d" % code)
            for event in out["events"]:
                data = event.get("data") or {}
                if owed is None or data.get("nonce") == owed:  # owed set: backlog pass
                    _report(base, data, *execute(data, binary, run))
            since, owed = max(since, out["seq"]), None
        except (OSError, ValueError) as err:
            print("review-bridge: %s (retrying)" % err, file=sys.stderr)
            time.sleep(5)
        if once:
            return


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--displayd", default=os.environ.get("DISPLAYD_URL", DEFAULT_URL))
    parser.add_argument("--review-bin", default=os.environ.get("REVIEW_BIN", "review"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("load").add_argument("bead")
    sub.add_parser("serve")
    args = parser.parse_args(argv)
    base = args.displayd.rstrip("/")
    if args.cmd == "serve":
        serve(base, args.review_bin)
        return 0
    try:
        load(base, args.bead, args.review_bin)
    except (OSError, ValueError) as err:
        print("review-bridge: %s" % err, file=sys.stderr)
        return 1
    print("review-bridge: %s is on the panel" % args.bead)
    return 0


if __name__ == "__main__":
    sys.exit(main())
