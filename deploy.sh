#!/bin/sh
# deploy.sh -- deliver this repo to the live lnx-server panel.
#
#   ./deploy.sh
#
# What it does, in order:
#   1. Records the panel's current renderer (to re-show after restart).
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
PRIOR=""
PRESTATE=$(mktemp); trap 'rm -f "$PRESTATE"' EXIT INT TERM
if curl -s -m 10 "http://$PANEL/state" -o "$PRESTATE"; then
    PRIOR=$(python3 -c \
        "import json,sys; print(json.load(open('$PRESTATE')).get('renderer') or '')")
    echo "prior view: ${PRIOR:-(blank)}"
else
    echo "warning: panel unreachable pre-deploy; continuing without prior view" >&2
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
        echo "deploy.sh: panel never became healthy" >&2; exit 1; }
    sleep 1
done
if [ -n "$PRIOR" ]; then
    if curl -s -m 10 -X POST "http://$PANEL/show" \
        -H 'Content-Type: application/json' \
        -d "{\"renderer\":\"$PRIOR\"}" | grep -q '"view"\|"renderer"'; then
        echo "re-showed $PRIOR"
    else
        echo "warning: could not re-show $PRIOR (may need params); continuing" >&2
    fi
fi
# Prove the build on the panel itself: RELOADED + SHA + commit QR, then
# auto-returns to the re-showed view (or the clock when none).
if curl -s -m 10 -X POST "http://$PANEL/reload" \
    -H 'Content-Type: application/json' \
    -d "{\"sha\":\"$SHA\"}" | grep -q '"view"'; then
    echo "reload confirmation showing on panel"
else
    echo "warning: /reload proof failed; continuing" >&2
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
    echo "deploy.sh: /deploy does not show $SHA: $GOT" >&2; exit 1; }
NOW=$(curl -s -m 10 "http://$PANEL/state")
RENDERER=$(printf '%s' "$NOW" | python3 -c \
    "import json,sys; print(json.load(sys.stdin).get('renderer') or '')")
[ -n "$RENDERER" ] || {
    echo "deploy.sh: panel shows nothing after deploy" >&2; exit 1; }
BL=$(printf '%s' "$NOW" | python3 -c \
    "import json,sys; b=json.load(sys.stdin)['screen']['backlight']; print(b.get('value') or 0)")
echo "panel: renderer=$RENDERER backlight=$BL stamp=$SHA"
echo "deployed $SHA at $DATE by $DEPLOYER"
