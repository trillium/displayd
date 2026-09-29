#!/bin/sh
# Install the displayd Mac-side bridges as a SET.
#
#   ./bridges/install-mac.sh                     deploy + restart + verify
#   ./bridges/install-mac.sh --check              verify only, change nothing
#   ./bridges/install-mac.sh --dir <dest>         deploy to <dest> (test/demo)
#   ./bridges/install-mac.sh --no-restart         deploy files, leave jobs alone
#   ./bridges/install-mac.sh --no-verify          deploy + restart, skip feed gate
#   ./bridges/install-mac.sh --with-obs           also bootstrap the new obs job
#
# What it does:
#   1. Copies EVERY file in bridges/mac-set.manifest from this repo to the
#      deployed bridges dir (default ~/.local/share/displayd) — one loop,
#      so a change to one file cannot be installed without the others.
#   2. Renders each managed launchd plist (bridges/*.plist) into
#      ~/Library/LaunchAgents with __DISPLAYD_DIR__ pointing at the
#      deployed dir, and restarts each managed job that is already loaded
#      (kickstart -k). Jobs never loaded before stay dormant — bootstrapping
#      a brand-new always-on job is a captain decision, not a side effect
#      (the obs-stream job additionally needs --with-obs plus OBS_PASSWORD
#      and OBS_SOURCE in the launchd environment).
#   3. Runs bridges/verify-mac-install.sh: set-integrity (deployed files
#      checksum-match this repo) plus the feed gate (each restarted job's
#      always-on feeds read warm in the daemon's /state). Any failure exits
#      nonzero and names what is wrong — a partial install is LOUD.
#
# What it does NOT do: touch the panel-host install (install.sh / systemd),
# restructure any bridge, or change the forgiving import guards' runtime
# behaviour (a missing preview must still never take state down — the fix
# is that a partial install is caught HERE, at install time).
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MANIFEST="$HERE/mac-set.manifest"
VERIFY="$HERE/verify-mac-install.sh"

DEST="$HOME/.local/share/displayd"
AGENTS_DIR="$HOME/Library/LaunchAgents"
DISPLAYD="http://100.81.88.113:8980"
DO_RESTART=1
DO_VERIFY=1
WITH_OBS=0
CHECK_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DEST="$2"; shift 2 ;;
        --agents-dir) AGENTS_DIR="$2"; shift 2 ;;
        --displayd) DISPLAYD="$2"; shift 2 ;;
        --no-restart) DO_RESTART=0; shift ;;
        --no-verify) DO_VERIFY=0; shift ;;
        --with-obs) WITH_OBS=1; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
        *) echo "unknown flag: $1 (see --help)" >&2; exit 2 ;;
    esac
done

# --check: verify the deployed set against this repo, changing nothing.
if [ "$CHECK_ONLY" -eq 1 ]; then
    exec "$VERIFY" --dir "$DEST" --displayd "$DISPLAYD"
fi

if [ ! -f "$MANIFEST" ]; then
    echo "install-mac: manifest missing: $MANIFEST" >&2
    exit 1
fi

mkdir -p "$DEST"
count=0
while read -r file _role; do
    case "$file" in ""|\#*) continue ;; esac
    if [ ! -f "$HERE/$file" ]; then
        echo "install-mac: repo file missing: $HERE/$file" >&2
        exit 1
    fi
    install -m 0644 "$HERE/$file" "$DEST/$file"
    count=$((count + 1))
done < "$MANIFEST"
echo "install-mac: deployed $count files -> $DEST"

# Render plist templates: __DISPLAYD_DIR__ always means the DEPLOYED dir,
# never a checkout — the jobs must survive worktrees coming and going.
mkdir -p "$AGENTS_DIR"
job_labels=""
while read -r file role; do
    case "$file" in ""|\#*) continue ;; esac
    case "$role" in job:*) label=$(expr "$role" : 'job:\(.*\)') ;; *) continue ;; esac
    job_labels="$job_labels $label"
    case "$label" in
        com.displayd.obs-stream-bridge)
            if [ "$WITH_OBS" -eq 0 ]; then
                echo "install-mac: $label not bootstrapped (needs --with-obs)"
                continue
            fi ;;
    esac
    tpl="$HERE/$label.plist"
    if [ ! -f "$tpl" ]; then
        echo "install-mac: plist template missing for $label" >&2
        exit 1
    fi
    # Templates address __DISPLAYD_DIR__/bridges/<file> (checkout layout);
    # the deployed dir is flat, so map onto it. The anchor keeps the
    # install prose inside the comments untouched.
    sed "s|__DISPLAYD_DIR__/bridges/|$DEST/|g" "$tpl" > "$AGENTS_DIR/$label.plist"
    if grep -q "<string>__DISPLAYD_DIR__" "$AGENTS_DIR/$label.plist"; then
        echo "install-mac: WARNING: unmapped placeholder left in a $label.plist path" >&2
    fi
    chmod 0644 "$AGENTS_DIR/$label.plist"
done < "$MANIFEST"

restarted=""
if [ "$DO_RESTART" -eq 1 ] && [ "$(uname)" = "Darwin" ]; then
    uid=$(id -u)
    for label in $job_labels; do
        case "$label" in
            com.displayd.obs-stream-bridge)
                if [ "$WITH_OBS" -eq 0 ]; then
                    continue
                fi ;;
        esac
        if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
            launchctl kickstart -k "gui/$uid/$label" 2>&1 || {
                echo "install-mac: restart failed for $label" >&2
                exit 1
            }
            echo "install-mac: restarted $label"
            restarted="$restarted $label"
        elif [ "$label" = "com.displayd.obs-stream-bridge" ] && [ "$WITH_OBS" -eq 1 ]; then
            launchctl bootstrap "gui/$uid" "$AGENTS_DIR/$label.plist" 2>&1 || {
                echo "install-mac: bootstrap failed for $label" >&2
                exit 1
            }
            echo "install-mac: bootstrapped $label"
            restarted="$restarted $label"
        else
            echo "install-mac: $label not loaded, leaving dormant (bootstrap: launchctl bootstrap gui/$uid $AGENTS_DIR/$label.plist)"
        fi
    done
    if [ "$WITH_OBS" -eq 1 ] && [ ! -f "$AGENTS_DIR/com.displayd.obs-stream-bridge.plist" ]; then
        echo "install-mac: WARNING: --with-obs without a rendered plist (template missing?)" >&2
    fi
elif [ "$DO_RESTART" -eq 1 ]; then
    echo "install-mac: not Darwin, skipping launchd restart (files + plists deployed)"
fi

if [ "$DO_VERIFY" -eq 1 ]; then
    if [ -n "$restarted" ]; then
        # Feed-gate exactly the jobs this install restarted: each one must
        # show its always-on feeds warm, or the install fails loudly.
        jobs_arg=$(echo "$restarted" | tr ' ' ',' | sed 's/^,//')
        sleep 8  # one poller tick + preview tick + margin before gating
        exec "$VERIFY" --dir "$DEST" --displayd "$DISPLAYD" --jobs "$jobs_arg"
    else
        # Nothing restarted (test dir, --no-restart, dormant jobs): still
        # gate set-integrity so a partial copy can never pass quietly.
        exec "$VERIFY" --dir "$DEST" --displayd "$DISPLAYD" --no-feeds
    fi
fi
echo "install-mac: done (--no-verify)"
