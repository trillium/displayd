#!/bin/sh
# deploy.sh -- deliver this repo to the live lnx-server panel.
#
#   ./deploy.sh
#
# What it does, in order:
#   1. Records the panel's current renderer (to re-show after restart).
#      A missing or transient prior (empty = blank panel, or a transient-only
#      view such as notice/reload) falls back to the clock instead: the
#      daemon's own return paths use the clock the same way, so a deploy
#      never skips the re-show and never parks the panel on a QR code.
#   2. rsyncs this repo to ~/displayd on lnx-server (no --delete: the host
#      holds host-local files the repo must never wipe -- touch.json,
#      state/, backups/, policy.json, the feedback log).
#   3. Restarts the daemon (sudo systemctl restart displayd).
#   4. Re-shows the prior view (restart blanks the screen), then POSTs
#      /reload with the deployed SHA so the panel itself proves the build
#      (transient RELOADED + SHA + commit QR, auto-returns to the view).
#   5. Writes the delivery stamp (UTC date + commit SHA + deployer) to the
#      host file ~/displayd/DEPLOYED, served panel-visible via GET /deploy
#      and the control page. The stamp is host-side only -- it is never
#      committed, so the repo stays clean.
#   6. Verifies: panel healthy, stamp shows this SHA, screen has content.
#   7. Restarts the touch service (it loads touch.json once at ITS OWN
#      startup, so a daemon-only restart leaves stale regions live) and
#      requires GET /touch/check to agree: drawn UI vs live regions, with
#      the exact differing rects on failure.
#
# Configuration (environment overrides):
#   DISPLAYD_HOST        ssh target (default trillium@lnx-server)
#   DISPLAYD_PANEL       panel API host:port (default 100.81.88.113:8980)
#   DISPLAYD_REMOTE_DIR  remote dir (default ~/displayd)
#   DEPLOYER             stamp author (default: local username)
#
# The API has no auth and stays tailnet-bound; this script changes no bind.
# No credentials, keys, or tokens live here or in the stamp.
set -eu

HOST="${DISPLAYD_HOST:-trillium@lnx-server}"
PANEL="${DISPLAYD_PANEL:-100.81.88.113:8980}"
REMOTE_DIR="${DISPLAYD_REMOTE_DIR:-~/displayd}"
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SSH="ssh -o ConnectTimeout=10 -o BatchMode=yes"

for cmd in ssh rsync curl python3 git; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "deploy.sh: required command '$cmd' not found" >&2; exit 1; }
done

SHA=$(git -C "$HERE" rev-parse HEAD)
case "$SHA" in
????????????????????????????????????????) ;; # 40-char SHA
*)
    echo "deploy.sh: bad HEAD sha: $SHA" >&2; exit 1;; esac
DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DEPLOYER="${DEPLOYER:-$(id -un)}"

echo "deploying $SHA ($DATE, by $DEPLOYER) to $HOST:$REMOTE_DIR"

# 1. Current renderer, to re-show after the restart blanks the screen.
#
# What counts as a real view (vs a transient) is derived from the repo
# itself, not a hardcoded list: transient kinds live in policy.PRIORITY
# ({attention, notice, reload}), but only kinds that ALSO name an
# installed renderer module are transient-only views. `attention` is a
# pull that displays the configured real view (chat), so it is correctly
# excluded -- the derivation yields exactly `notice reload`. The live
# /state snapshot's policy.transient.active is honoured too, so a
# transient caught mid-flight is treated as no prior even if the
# renderer field lags. Either way the fallback is the clock -- the same
# known-good default the daemon's own return paths
# (_transient_expired, _return_from_base, dismiss_reload) use when there
# is no base view to go back to.
DEFAULT_VIEW="clock"
# One quoted argument with real newlines (see the RELOAD_BODY note below):
# backslash-continued "..." segments arrive as separate argv words, so this
# derivation ran only `import sys,os` and printed nothing, silently living on
# the `|| echo` fallback -- the repo-derived set its comment promises was
# never computed.
TRANSIENT_VIEWS=$(HERE="$HERE" python3 -c '
import os, sys
here = os.environ["HERE"]
sys.path.insert(0, here)
import policy
renders = {f[:-3] for f in os.listdir(os.path.join(here, "renderers"))
           if f.endswith(".py")}
print(" ".join(sorted(k for k in policy.PRIORITY if k in renders)))
' 2>/dev/null || echo "notice reload")
[ -n "$TRANSIENT_VIEWS" ] || TRANSIENT_VIEWS="notice reload"
is_transient_view() {
    case " $TRANSIENT_VIEWS " in *" $1 "*) return 0;; *) return 1;; esac
}
# Best-effort restore: put a real view back before a loud failure exit,
# so a failed deploy never leaves the jumbotron blank. Never fails the
# deploy further -- the caller still exits non-zero after this.
restore_panel() {
    if curl -s -m 10 -X POST "http://$PANEL/show" \
        -H 'Content-Type: application/json' \
        -d "{\"renderer\":\"$1\"}" | grep -q '"view"\|"renderer"'; then
        echo "restored $1 before exit"
    else
        echo "warning: could not restore $1 before exit" >&2
    fi
}
PRIOR=""
PRE_TRANSIENT=""
PRESTATE=$(mktemp); trap 'rm -f "$PRESTATE"' EXIT INT TERM
if curl -s -m 10 "http://$PANEL/state" -o "$PRESTATE"; then
    PRIOR=$(python3 -c \
        "import json; print(json.load(open('$PRESTATE')).get('renderer') or '')" 2>/dev/null || true)
    # One quoted argument (same reason as TRANSIENT_VIEWS above): split
    # segments left this always empty, so the "transient active" fallback
    # reason below could never fire.
    PRE_TRANSIENT=$(PRESTATE="$PRESTATE" python3 -c '
import json, os
d = json.load(open(os.environ["PRESTATE"]))
print(((d.get("policy") or {}).get("transient") or {}).get("active") or "")
' 2>/dev/null || true)
    FALLBACK_REASON=""
    if [ -z "$PRIOR" ]; then
        FALLBACK_REASON="empty (panel blank)"
    elif is_transient_view "$PRIOR"; then
        FALLBACK_REASON="transient view ($PRIOR)"
    elif [ -n "$PRE_TRANSIENT" ] && is_transient_view "$PRE_TRANSIENT"; then
        FALLBACK_REASON="transient active ($PRE_TRANSIENT showing $PRIOR)"
    fi
    if [ -n "$FALLBACK_REASON" ]; then
        echo "prior view $FALLBACK_REASON; falling back to $DEFAULT_VIEW"
        PRIOR="$DEFAULT_VIEW"
    else
        echo "prior view: $PRIOR"
    fi
else
    echo "warning: panel unreachable pre-deploy; falling back to $DEFAULT_VIEW" >&2
    PRIOR="$DEFAULT_VIEW"
fi

# 2. Sync the repo. Host-state exclusions: DEPLOYED/policy.json/feedback
#    logs are written on the host and must survive redeploys; AppleDouble
#    (._*) and caches never cross to Linux.
rsync -az \
    --exclude '.git/' \
    --exclude '.pi/' \
    --exclude '__pycache__/' \
    --exclude '*.py[cod]' \
    --exclude '.pytest_cache/' \
    --exclude '.ruff_cache/' \
    --exclude '.mypy_cache/' \
    --exclude '.venv/' \
    --exclude 'venv/' \
    --exclude '.DS_Store' \
    --exclude '._*' \
    --exclude 'DEPLOYED' \
    --exclude 'policy.json' \
    --exclude '*.jsonl' \
    --exclude '*_frames/' \
    "$HERE/" "$HOST:$REMOTE_DIR/"
echo "rsync done"

# 3. Restart the daemon. -n fails fast instead of hanging on a password
#    prompt; BatchMode fails fast on ssh approval walls.
$SSH "$HOST" 'sudo -n systemctl restart displayd'
echo "daemon restarted"

# 4. Wait for health, then re-show the prior view (never leave it black).
i=0
until curl -s -m 5 "http://$PANEL/health" | grep -q '"ok"'; do
    i=$((i + 1)); [ "$i" -ge 30 ] && {
        echo "deploy.sh: panel never became healthy" >&2
        restore_panel "$PRIOR"
        exit 1; }
    sleep 1
done
# PRIOR is always a real view here (step 1 falls back to the clock), so
# the re-show always runs -- it is never skipped and never parks a
# transient. A failed re-show warns LOUDLY but continues: the strict
# verify in step 6 still fails the deploy, and the touch step below is
# always reached.
reshow() {
    curl -s -m 10 -X POST "http://$PANEL/show" \
        -H 'Content-Type: application/json' \
        -d "{\"renderer\":\"$1\"}" | grep -q '"view"\|"renderer"'
}
if reshow "$PRIOR"; then
    echo "re-showed $PRIOR"
elif [ "$PRIOR" != "$DEFAULT_VIEW" ] && reshow "$DEFAULT_VIEW"; then
    echo "warning: could not re-show $PRIOR (may need params); showing $DEFAULT_VIEW instead" >&2
    PRIOR="$DEFAULT_VIEW"
else
    echo "warning: could not re-show $PRIOR (may need params); continuing" >&2
fi
# Prove the build on the panel itself: RELOADED + SHA + commit QR, then
# auto-returns to the re-showed view (or the clock when none). The host
# cannot read git history (its .git is a gitdir pointer at a MacBook
# path), so the highlights summary is extracted here where git works and
# forwarded with the proof request: bounded structural extraction
# (subject + a few body lines, no model) via
# renderers/reload_highlights.py. Empty extraction posts sha alone --
# the panel then renders the classic view, exactly as before.
HIGHLIGHTS=""
if MSG=$(git -C "$HERE" log -1 --format=%B "$SHA" 2>/dev/null); then
    HIGHLIGHTS=$(printf '%s' "$MSG" \
        | python3 "$HERE/renderers/reload_highlights.py" 2>/dev/null || true)
fi
# ONE quoted argument, newlines inside it: a backslash-continued run of
# separate "..." segments is NOT one argv word (verified across sh/dash/
# bash/zsh), so python3 -c silently ran only the FIRST segment -- the rest
# landed in sys.argv -- and printed nothing. RELOAD_BODY came out EMPTY, so
# the proof POSTed an empty body, the daemon answered 400 "sha is
# required", and the step warned and skipped on EVERY deploy. This is the
# same idiom the touch probe below already uses (one quoted string with real
# newlines); keep every python3 -c in this file that way.
RELOAD_BODY=$(SHA="$SHA" HIGHLIGHTS="$HIGHLIGHTS" python3 -c '
import json, os
body = {"sha": os.environ["SHA"]}
hl = os.environ.get("HIGHLIGHTS", "").strip()
if hl:
    body["highlights"] = hl
print(json.dumps(body))
')
# An unbuildable body would otherwise become an empty POST and a vague
# warning: that is the silent skip this step exists to prevent, so it is a
# hard deploy failure instead of a skipped proof.
if [ -z "$RELOAD_BODY" ]; then
    echo "deploy.sh: could not build the /reload proof body (sha=$SHA)" >&2
    restore_panel "$PRIOR"
    exit 1
fi
if [ -n "$HIGHLIGHTS" ]; then
    echo "reload highlights: $(printf '%s' "$HIGHLIGHTS" | head -n 1)"
else
    echo "reload highlights: none (classic SHA+QR view)"
fi
if curl -s -m 10 -X POST "http://$PANEL/reload" \
    -H 'Content-Type: application/json' \
    -d "$RELOAD_BODY" | grep -q '"view"'; then
    echo "reload confirmation showing on panel"
else
    echo "warning: /reload proof failed; continuing" >&2
fi
# Return from the proof to the real view: /reload stays up indefinitely
# until confirmed, and the deploy must not report success while showing
# a QR code (nor park transients via the re-show above). Confirming
# returns to the re-showed base view; a manual /show of PRIOR is the
# fallback when the confirm path misses (e.g. no reload active).
if curl -s -m 10 -X POST "http://$PANEL/reload/confirm" \
    -H 'Content-Type: application/json' \
    -d '{"via":"tap"}' | grep -q '"confirmed": *true'; then
    echo "reload confirmed, back to $PRIOR"
elif reshow "$PRIOR"; then
    echo "returned to $PRIOR after reload proof"
else
    echo "warning: could not return to $PRIOR after reload proof; continuing" >&2
fi

# 5. Delivery stamp, host-side only (never committed to the repo).
STAMP_JSON=$(python3 -c \
    "import json; print(json.dumps({'date':'$DATE','sha':'$SHA','deployer':'$DEPLOYER'}))")
printf '%s' "$STAMP_JSON" | $SSH "$HOST" "cat > $REMOTE_DIR/DEPLOYED"
# Continuity with the previous ad-hoc convention (plain SHA file).
printf '%s\n' "$SHA" | $SSH "$HOST" "cat > $REMOTE_DIR/.displayd-release"
echo "stamp written: $STAMP_JSON"

# 6. Verify: stamp serves this SHA, screen shows content, backlight sane.
GOT=$(curl -s -m 10 "http://$PANEL/deploy")
echo "$GOT" | grep -q "$SHA" || {
    echo "deploy.sh: /deploy does not show $SHA: $GOT" >&2
    restore_panel "$PRIOR"
    exit 1; }
NOW=$(curl -s -m 10 "http://$PANEL/state")
RENDERER=$(printf '%s' "$NOW" | python3 -c \
    "import json,sys; print(json.load(sys.stdin).get('renderer') or '')")
# A transient passes a truthiness test, so assert a REAL view: non-empty
# and not a transient-only view. On failure restore the real view first
# (loud exit, usable panel) instead of leaving whatever is showing.
if [ -z "$RENDERER" ]; then
    echo "deploy.sh: panel shows nothing after deploy" >&2
    restore_panel "$PRIOR"
    exit 1
elif is_transient_view "$RENDERER"; then
    echo "deploy.sh: panel shows transient '$RENDERER' after deploy (expected a real view)" >&2
    restore_panel "$PRIOR"
    exit 1
fi
BL=$(printf '%s' "$NOW" | python3 -c \
    "import json,sys; b=json.load(sys.stdin)['screen']['backlight']; print(b.get('value') or 0)")
echo "panel: renderer=$RENDERER backlight=$BL stamp=$SHA"

# 7. Touch freshness: restart the touch unit so it re-announces its
#    effective regions, then require proof it is genuinely up AND
#    announcing -- ActiveState=active first (a wedged `deactivating` reads
#    as "restarting" to is-active pollers), then a /touch/check whose
#    announced_at is NEWER than the pre-restart heartbeat (the new process
#    announced, not a stale heartbeat). Anything else fails the deploy
#    loudly: a silent wedge means dead panel input.
#    The panel is already re-showed above, so a failing gate never leaves
#    it black -- it fails the deploy loudly instead of shipping drift.
TOUCH_BEFORE=$(curl -s -m 5 "http://$PANEL/touch/check" | python3 -c \
    "import json,sys; print(json.load(sys.stdin).get('announced_at') or '')" 2>/dev/null || true)
if $SSH "$HOST" 'sudo -n systemctl restart displayd-touch'; then
    TOUCH_RESTARTED=1
    echo "touch service restarted"
else
    TOUCH_RESTARTED=0
    echo "warning: displayd-touch restart failed (unit not installed?)" >&2
fi
if [ "$TOUCH_RESTARTED" = "1" ]; then
    i=0
    TOUCH_STATE=""
    until [ "$TOUCH_STATE" = "active" ] || [ "$i" -ge 15 ]; do
        i=$((i + 1)); sleep 1
        TOUCH_STATE=$($SSH "$HOST" 'systemctl is-active displayd-touch' 2>/dev/null || true)
    done
    if [ "$TOUCH_STATE" != "active" ]; then
        echo "deploy.sh: displayd-touch never reached active after restart (state: ${TOUCH_STATE:-unknown}); touch input may be dead" >&2
        exit 1
    fi
    echo "touch service active"
fi
# Top-level "ok" only: per-view entries carry their own "ok", so a
# grep would match a nested agreement while the matrix disagrees.
CHECK_OK=""; CHECK_STATUS=""; CHECK_FRESH=""
check_probe() {
    CHECK=$(curl -s -m 5 "http://$PANEL/touch/check" || true)
    PROBE=$(printf '%s' "$CHECK" | TOUCH_BEFORE="$TOUCH_BEFORE" python3 -c \
        "import json,os,sys
try:
    r = json.load(sys.stdin)
except Exception:
    print('PARSE_FAIL PARSE_FAIL PARSE_FAIL'); raise SystemExit
before = os.environ.get('TOUCH_BEFORE') or ''
try:
    fresh = (not before) or float(r.get('announced_at') or 0) > float(before)
except (TypeError, ValueError):
    fresh = False
print(r.get('ok'), r.get('status'), fresh)" 2>/dev/null || echo "PARSE_FAIL")
    CHECK_OK=$(printf '%s' "$PROBE" | cut -d' ' -f1)
    CHECK_STATUS=$(printf '%s' "$PROBE" | cut -d' ' -f2)
    CHECK_FRESH=$(printf '%s' "$PROBE" | cut -d' ' -f3)
    [ "$CHECK_OK" = "True" ] && [ "$CHECK_FRESH" = "True" ]
}
i=0
until check_probe || [ "$i" -ge 15 ]; do
    i=$((i + 1)); sleep 1
done
if [ "$CHECK_OK" = "True" ] && [ "$CHECK_FRESH" = "True" ]; then
    echo "touch regions agree (fresh heartbeat): $(printf '%s' "$CHECK" | python3 -c \
        "import json,sys; r=json.load(sys.stdin); print(r.get('current_view'), '-', r.get('status'))")"
elif [ "$TOUCH_RESTARTED" = "0" ] && [ "$CHECK_STATUS" = "unknown" ]; then
    echo "warning: no touch heartbeat and no unit to restart; continuing without the touch check" >&2
elif [ "$CHECK_OK" = "True" ]; then
    echo "touch region check failed: $CHECK" >&2
    echo "deploy.sh: /touch/check agrees but the heartbeat is stale (no re-announce after restart); touch input may be dead" >&2
    exit 1
elif [ "$CHECK_STATUS" = "stale" ] || [ "$CHECK_STATUS" = "unknown" ]; then
    echo "touch region check failed: $CHECK" >&2
    echo "deploy.sh: touch heartbeat is blind ($CHECK_STATUS) even after restart; touch input (and its drift gate) may be dead" >&2
    exit 1
else
    echo "touch region check failed: $CHECK" >&2
    echo "deploy.sh: drawn UI and live touch regions disagree (GET /touch/check); fix touch.json and re-run" >&2
    exit 1
fi
echo "deployed $SHA at $DATE by $DEPLOYER"
