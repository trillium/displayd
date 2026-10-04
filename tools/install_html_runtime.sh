#!/bin/sh
# install_html_runtime.sh -- put the optional html view's native runtime and
# its trusted templates in place on a displayd target, then verify the set.
#
#   tools/install_html_runtime.sh --prefix /opt/displayd
#   tools/install_html_runtime.sh --prefix /opt/displayd --check
#   tools/install_html_runtime.sh --prefix /opt/displayd --strict
#
# This script is the ONLY owner of that artifact set. A clean target that
# skipped it has no layout engine and no templates, which the view can only
# report as a red card -- so "installed" is never claimed here unless --check
# agrees, and --check is the same list the renderer itself probes.
#
# Layout, relative to --prefix:
#
#   renderers/native/liblitehtmlpil.{dylib,so}   the built engine
#   renderers/native/displayd_html.h             the C ABI both halves agree on
#   renderers/native/pil_container.{h,cpp}        the litehtml container
#   renderers/native/shim.cpp                    the exported entry points
#   renderers/native/LICENSE-litehtml             BSD-3-Clause, must travel
#   renderers/native/LICENSE-gumbo                Apache-2.0, must travel
#   html-templates/*.html                         the trusted templates
#   tools/build_litehtml.sh                       the reproducible rebuild
#
# Those are exactly the paths _html_native.lib_path() and
# _html_templates.default_root() resolve, both derived from the installed
# renderers/ directory -- so an installed tree finds them with no environment
# variable, no developer-local path, and no source checkout.
#
# The engine is a compiled artifact for one platform, so it is BUILT on the
# target from the pinned upstream revision (tools/build_litehtml.sh; see
# docs/HTML_RENDERER.md) and never copied between machines. Everything else is
# tracked in git and copies verbatim. A network is needed only when the engine
# has never been built on that target.
#
# Exit status:
#   0  the set is complete
#   1  installation failed, or the set is incomplete and --strict was asked
#      for. Without --strict a target that cannot build (no cmake/compiler)
#      still installs every other artifact, exits 0, and says exactly what to
#      run by hand -- an optional view must not be able to fail a deploy.
#
# Output (stdout, both modes, so one caller reads one shape):
#   complete: <prefix>            every required artifact is in place
#   missing: <relative path>      one line per gap, naming the path
#   incomplete: <prefix>          printed after the missing lines
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(dirname "$HERE")
PREFIX=$REPO
CHECK=0
STRICT=0

log() { printf 'install_html_runtime: %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) [ $# -ge 2 ] || die "--prefix needs a directory"
                  PREFIX=$2; shift 2 ;;
        --prefix=*) PREFIX=${1#--prefix=}; shift ;;
        --check) CHECK=1; shift ;;
        --strict) STRICT=1; shift ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) die "unknown argument $1" ;;
    esac
done
[ -n "$PREFIX" ] || die "--prefix cannot be empty"

# The tracked half of the set: everything the renderer reads or a rebuild
# needs, and nothing platform-specific. The engine and the templates are
# globbed separately because either may legitimately be absent in a checkout.
required_paths() {
    cat <<'EOF'
renderers/native/displayd_html.h
renderers/native/pil_container.h
renderers/native/pil_container.cpp
renderers/native/shim.cpp
renderers/native/LICENSE-litehtml
renderers/native/LICENSE-gumbo
tools/build_litehtml.sh
EOF
}

# Mode 0755 only for what is executed; everything else is read as data.
mode_for() {
    case "$1" in
        *.sh) echo 0755 ;;
        *) echo 0644 ;;
    esac
}

# The engine, whichever suffix this platform uses. Both are accepted because
# _html_native.lib_path() tries both in the same order.
lib_name() {
    for suffix in dylib so; do
        if [ -f "$1/renderers/native/liblitehtmlpil.$suffix" ]; then
            echo "renderers/native/liblitehtmlpil.$suffix"
            return 0
        fi
    done
    return 1
}

template_count() {
    find "$1/html-templates" -maxdepth 1 -name '*.html' 2>/dev/null | wc -l | tr -d ' '
}

# One machine-readable line per gap, so a caller can print them verbatim and a
# test can assert on the exact path rather than on prose.
report_missing() {
    required_paths | while read -r rel; do
        [ -f "$PREFIX/$rel" ] || echo "missing: $rel"
    done
    lib_name "$PREFIX" >/dev/null \
        || echo "missing: renderers/native/liblitehtmlpil.{so,dylib}"
    [ "$(template_count "$PREFIX")" -ge 1 ] \
        || echo "missing: html-templates/*.html"
}

if [ "$CHECK" -eq 1 ]; then
    misses=$(report_missing)
    if [ -z "$misses" ]; then
        echo "complete: $PREFIX"
        exit 0
    fi
    printf '%s\n' "$misses"
    echo "incomplete: $PREFIX"
    [ "$STRICT" -eq 1 ] && exit 1
    exit 0
fi

mkdir -p "$PREFIX/renderers/native" "$PREFIX/html-templates" "$PREFIX/tools"

for rel in $(required_paths); do
    src=$REPO/$rel
    [ -f "$src" ] || die "source missing: $src (run this from a displayd checkout)"
    install -m "$(mode_for "$rel")" "$src" "$PREFIX/$rel"
done

for template in "$REPO"/html-templates/*.html; do
    [ -e "$template" ] || die "no templates to install in $REPO/html-templates"
    install -m 0644 "$template" "$PREFIX/html-templates/"
done

# The engine. Already built in this checkout, or build it here -- never copy
# a binary built for a different platform.
if ! built=$(lib_name "$REPO"); then
    log "no engine in $REPO/renderers/native; building it on this machine"
    if "$REPO/tools/build_litehtml.sh"; then
        built=$(lib_name "$REPO") || die "the build reported success but produced no library"
    else
        log "WARNING: could not build the layout engine."
        log "WARNING: the html view will draw a build card until it exists:"
        log "WARNING:   $PREFIX/tools/build_litehtml.sh"
        [ "$STRICT" -eq 1 ] && die "html runtime incomplete and --strict was asked for"
        built=""
    fi
fi
if [ -n "$built" ]; then
    install -m 0755 "$REPO/$built" "$PREFIX/$built"
fi

misses=$(report_missing)
if [ -n "$misses" ]; then
    printf '%s\n' "$misses"
    echo "incomplete: $PREFIX"
    [ "$STRICT" -eq 1 ] && exit 1
    exit 0
fi
# Both modes end on the same two tokens, so a caller reads one shape whether
# it installed or merely checked. Install mode's prose goes to stderr.
log "html runtime installed at $PREFIX ($(lib_name "$PREFIX"), $(template_count "$PREFIX") template(s))"
echo "complete: $PREFIX"