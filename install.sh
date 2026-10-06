#!/bin/sh
# Install displayd onto a Linux box that has a framebuffer console.
#
#   sudo ./install.sh              installs into /opt/displayd
#   sudo ./install.sh /usr/local/lib/displayd
#   sudo DISPLAYD_SKIP_HTML=1 ./install.sh    skip the html view's C++ runtime
#
# The daemon needs no build step: this copies the source and renderers into
# place, writes the systemd unit with that path baked in, and starts it.
#
# One thing does get built: the optional html view's native layout engine.
# It is a per-platform compiled artifact, so it is compiled on this box by
# the shipped build script (pinned upstream revision, no vendor branch) rather
# than copied from whoever built the checkout. See tools/install_html_runtime.sh
# for the artifact set and docs/HTML_RENDERER.md for the contract.
set -eu

PREFIX="${1:-/opt/displayd}"
UNIT=/etc/systemd/system/displayd.service
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh must run as root: it installs a systemd unit." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required." >&2
    exit 1
fi

mkdir -p "$PREFIX/renderers"
# The daemon imports its siblings by bare name (policy, playlist, feedback and
# their helpers), so the whole top-level module set has to land here: a prefix
# holding only displayd.py cannot even import it.
install -m 0644 "$HERE"/*.py "$PREFIX/"
install -m 0644 "$HERE"/renderers/*.py "$PREFIX/renderers/"
# The component layer is part of the shipped UI, not a developer extra: every
# view imports it by name ("from ui import tile"), and the daemon's playlist
# imports its bar component, so a prefix without renderers/ui/ cannot load the
# picker, the home screen or the progress bar at all.
mkdir -p "$PREFIX/renderers/ui"
install -m 0644 "$HERE"/renderers/ui/*.py "$PREFIX/renderers/ui/"
# Shipped renderer data (e.g. renderers/beads_stores.json): install whatever
# exists, without failing when there is nothing to copy.
for extra in "$HERE"/renderers/*.json; do
    [ -e "$extra" ] && install -m 0644 "$extra" "$PREFIX/renderers/"
done
install -m 0644 "$HERE/README.md" "$PREFIX/README.md"
install -m 0644 "$HERE/requirements.txt" "$PREFIX/requirements.txt"

# The html view's runtime set: the built engine, the native sources and
# licences it was built from, the trusted templates, and the build script so
# a later rebuild works from the installed tree alone. Strict, because a clean
# target that ends up without the engine only discovers it as a red card on the
# panel; DISPLAYD_SKIP_HTML=1 is the documented opt-out for a box that will
# never run the html view. Runs BEFORE the unit is written, so a failure stops
# here with nothing half-installed and nothing started.
if [ "${DISPLAYD_SKIP_HTML:-0}" = "1" ]; then
    echo "warning: DISPLAYD_SKIP_HTML=1 -- skipping the html runtime." >&2
    echo "warning: the html view will draw a build card; install it later with" >&2
    echo "  $PREFIX/tools/build_litehtml.sh" >&2
else
    sh "$HERE/tools/install_html_runtime.sh" --prefix "$PREFIX" --strict
fi

# The unit ships with the default path; rewrite it when a different prefix is used.
sed "s|/opt/displayd|$PREFIX|g" "$HERE/displayd.service" > "$UNIT"
chmod 0644 "$UNIT"

if ! python3 -c "import PIL" >/dev/null 2>&1; then
    echo "warning: Pillow is not importable. Install it before starting:" >&2
    echo "  python3 -m pip install -r $HERE/requirements.txt" >&2
fi

systemctl daemon-reload
systemctl enable --now displayd.service
echo "displayd installed at $PREFIX and started."
echo "Check it with: curl -s localhost:8980/health"
