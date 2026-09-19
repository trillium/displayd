#!/bin/sh
# Install displayd onto a Linux box that has a framebuffer console.
#
#   sudo ./install.sh              installs into /opt/displayd
#   sudo ./install.sh /usr/local/lib/displayd
#
# The daemon needs no build step: this copies the source and renderers into
# place, writes the systemd unit with that path baked in, and starts it.
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
install -m 0644 "$HERE/displayd.py" "$PREFIX/displayd.py"
install -m 0644 "$HERE"/renderers/*.py "$PREFIX/renderers/"
# Shipped renderer data (e.g. renderers/beads_stores.json): install whatever
# exists, without failing when there is nothing to copy.
for extra in "$HERE"/renderers/*.json; do
    [ -e "$extra" ] && install -m 0644 "$extra" "$PREFIX/renderers/"
done
install -m 0644 "$HERE/README.md" "$PREFIX/README.md"

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
