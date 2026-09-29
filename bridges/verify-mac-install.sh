#!/bin/sh
# Verify a Mac-side bridge install — the part of install-mac.sh that matters
# most. Exits 0 only when the deployed set is whole AND its always-on feeds
# are warm; any silence (missing file, stale copy, cold feed) exits nonzero
# and NAMES what is wrong.
#
#   ./bridges/verify-mac-install.sh [--dir DEST] [--displayd BASE]
#                                   [--jobs l1,l2] [--no-feeds]
#                                   [--state-json FILE]
#
# Gates:
#   1. SET-INTEGRITY (always): every file in mac-set.manifest exists in DEST
#      and sha256-matches this repo. A withheld or stale file fails HERE —
#      this is what catches the task-va5wb partial install at install time.
#   2. FEED GATE (unless --no-feeds): for each job under test, its
#      always-on feeds must read "warm" in GET <displayd>/state:
#        com.displayd.macos-state-bridge -> macbook/state + macbook/preview
#        com.displayd.talon-apps-bridge  -> talon_apps/state
#      Event-driven feeds are REPORTED, never gated: chat/message,
#      activity/event, stream/frame and macbook/zoom are legitimately cold
#      when nobody chats / nothing happened / nobody tapped — requiring
#      them warm would cry wolf on every quiet night.
#      Which jobs are tested: --jobs l1,l2, else every manifest job whose
#      label is currently loaded in launchd (gui/$UID). --state-json reads
#      a saved /state document instead of HTTP (demos, tests).
#
# Does NOT change the forgiving import guards: a missing preview must still
# never take state down at runtime. The guard stays; the silence goes.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MANIFEST="$HERE/mac-set.manifest"

DEST="$HOME/.local/share/displayd"
DISPLAYD="http://100.81.88.113:8980"
JOBS=""
NO_FEEDS=0
STATE_JSON=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DEST="$2"; shift 2 ;;
        --displayd) DISPLAYD="$2"; shift 2 ;;
        --jobs) JOBS="$2"; shift 2 ;;
        --no-feeds) NO_FEEDS=1; shift ;;
        --state-json) STATE_JSON="$2"; shift 2 ;;
        -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
        *) echo "unknown flag: $1 (see --help)" >&2; exit 2 ;;
    esac
done

fail=0

# --- Gate 1: set integrity -------------------------------------------------
missing=0
while read -r file _role; do
    case "$file" in ""|\#*) continue ;; esac
    if [ ! -f "$DEST/$file" ]; then
        echo "VERIFY FAIL: $file missing in $DEST (partial install)" >&2
        missing=1; fail=1
        continue
    fi
    a=$(shasum -a 256 "$HERE/$file" | cut -d' ' -f1)
    b=$(shasum -a 256 "$DEST/$file" | cut -d' ' -f1)
    if [ "$a" != "$b" ]; then
        echo "VERIFY FAIL: $file in $DEST is STALE (differs from repo)" >&2
        fail=1
    fi
done < "$MANIFEST"
if [ "$missing" -eq 0 ] && [ "$fail" -eq 0 ]; then
    n=$(grep -cv '^[[:space:]]*\(#\|$\)' "$MANIFEST")
    echo "VERIFY OK: set integrity — $n files match the repo"
fi

if [ "$NO_FEEDS" -eq 1 ]; then
    [ "$fail" -eq 0 ] && echo "VERIFY PASS (feeds skipped via --no-feeds)"
    exit "$fail"
fi

# --- Which jobs are under test ---------------------------------------------
if [ -z "$JOBS" ]; then
    if [ "$(uname)" = "Darwin" ]; then
        uid=$(id -u)
        probed=""
        while read -r _file role; do
            case "$role" in job:*) label=$(expr "$role" : 'job:\(.*\)') ;; *) continue ;; esac
            if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
                probed="$probed,$label"
            fi
        done < "$MANIFEST"
        JOBS=$(echo "$probed" | sed 's/^,//')
    fi
    if [ -z "$JOBS" ]; then
        echo "VERIFY FAIL: no jobs under test (none loaded, no --jobs given)" >&2
        exit 1
    fi
fi

# --- Gate 2: feed warmth ----------------------------------------------------
if [ -n "$STATE_JSON" ]; then
    state_src="$STATE_JSON"
else
    state_src=$(mktemp)
    trap 'rm -f "$state_src"' EXIT INT TERM
    if ! curl -sf --max-time 10 "$DISPLAYD/state" -o "$state_src"; then
        echo "VERIFY FAIL: daemon unreachable at $DISPLAYD/state" >&2
        exit 1
    fi
fi

JOBS_CSV="$JOBS" STATE_SRC="$state_src" python3 - <<'EOF'
import csv, io, json, os, sys

with open(os.environ["STATE_SRC"]) as fh:
    try:
        state = json.load(fh)
    except ValueError as err:
        print("VERIFY FAIL: /state is not JSON (%s)" % err)
        sys.exit(1)
feeds = state.get("feeds", {})

# Always-on pollers: loaded job with a cold feed is the incident replayed.
REQUIRED = {
    "com.displayd.macos-state-bridge": [("macbook", "state"),
                                        ("macbook", "preview")],
    "com.displayd.talon-apps-bridge": [("talon_apps", "state")],
}
# Event-driven / on-demand: reported for the human, never gated.
REPORT = [("chat", "message"), ("activity", "event"),
          ("stream", "frame"), ("macbook", "zoom")]

jobs = next(csv.reader(io.StringIO(os.environ["JOBS_CSV"])))
jobs = [j.strip() for j in jobs if j.strip()]
fail = False
for label in jobs:
    for renderer, inp in REQUIRED.get(label, []):
        health = (feeds.get(renderer, {}).get(inp, {}) or {}).get("health")
        age = (feeds.get(renderer, {}).get(inp, {}) or {}).get("age_seconds")
        if health == "warm":
            print("VERIFY OK: %s/%s warm (age %ss) for %s"
                  % (renderer, inp, age, label))
        else:
            print("VERIFY FAIL: %s/%s is '%s' (age %s) — %s degraded SILENTLY"
                  % (renderer, inp, health, age, label))
            fail = True
for renderer, inp in REPORT:
    health = (feeds.get(renderer, {}).get(inp, {}) or {}).get("health")
    age = (feeds.get(renderer, {}).get(inp, {}) or {}).get("age_seconds")
    print("VERIFY INFO: %s/%s %s (age %s) — event-driven, report-only"
          % (renderer, inp, health, age))
sys.exit(1 if fail else 0)
EOF
rc=$?
if [ "$rc" -eq 0 ] && [ "$fail" -eq 0 ]; then
    echo "VERIFY PASS: set whole, feeds warm"
fi
exit $((rc || fail))
