#!/usr/bin/env bash
#
# Build the displayd html renderer's native half: litehtml (layout) plus our
# own PIL-backed document_container behind a C ABI.
#
#   tools/build_litehtml.sh            # build (downloads litehtml on first run)
#   tools/build_litehtml.sh --check    # report what is installed, build nothing
#   tools/build_litehtml.sh --force    # rebuild even if the library looks current
#
# Reproducible by construction: the upstream revision is pinned to a full
# commit SHA (not a branch, not "latest"), the build is Release with
# assertions off, and nothing but the two vendored-in-tree sources is
# compiled. No network access is needed at render time, ever.
#
# The result is a single shared library next to this script's target dir,
# gitignored, because it is a build artifact:
#
#   renderers/native/liblitehtmlpil.{dylib,so}
#
# Requirements: cmake, a C++17 compiler, git. Nothing else -- no fontconfig,
# no image libraries. PIL stays on the Python side, where it already is.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
NATIVE="$ROOT/renderers/native"
BUILD_DIR="${DISPLAYD_LITEHTML_BUILD:-$ROOT/build/litehtml}"

# Pinned upstream revision. Bump deliberately, then re-run the tests in
# tests/test_html.py -- the C ABI is written against this tree.
LITEHTML_REPO="https://github.com/litehtml/litehtml"
LITEHTML_REV="5624e795be50f02c21c89985c374dcd659dbd74b"

case "$(uname -s)" in
    Darwin) LIB_SUFFIX="dylib" ;;
    Linux)  LIB_SUFFIX="so" ;;
    *)      echo "build_litehtml: unsupported platform $(uname -s)" >&2; exit 1 ;;
esac
LIB="$NATIVE/liblitehtmlpil.$LIB_SUFFIX"

log() { printf 'build_litehtml: %s\n' "$*" >&2; }
die() { printf 'build_litehtml: %s\n' "$*" >&2; exit 1; }

check_only=0
force=0
for arg in "$@"; do
    case "$arg" in
        --check) check_only=1 ;;
        --force) force=1 ;;
        -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "unknown argument $arg" ;;
    esac
done

if [ "$check_only" -eq 1 ]; then
    if [ -f "$LIB" ]; then
        log "installed: $LIB"
        python3 - "$LIB" <<'PY' 2>/dev/null || true
import ctypes, sys
lib = ctypes.CDLL(sys.argv[1])
lib.lhtml_version.restype = ctypes.c_char_p
print("build_litehtml: version: %s" % lib.lhtml_version().decode())
PY
    else
        log "not built: $LIB"
        log "run tools/build_litehtml.sh to build it"
    fi
    exit 0
fi

[ "$force" -eq 0 ] && [ -f "$LIB" ] && { log "already built: $LIB (use --force to rebuild)"; exit 0; }

for tool in cmake git; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required but not on PATH"
done
CXX_BIN="${CXX:-}"
if [ -z "$CXX_BIN" ]; then
    for candidate in c++ clang++ g++; do
        if command -v "$candidate" >/dev/null 2>&1; then CXX_BIN="$candidate"; break; fi
    done
fi
[ -n "$CXX_BIN" ] || die "no C++ compiler found (set CXX)"

# The reference container in containers/test/ is deliberately not built or
# vendored: it is stale against these headers and its delete_font is a no-op.
mkdir -p "$NATIVE" "$BUILD_DIR"

# ------------------------------------------------------------------ fetch
SRC="$BUILD_DIR/litehtml"
if [ ! -d "$SRC/.git" ]; then
    log "cloning litehtml $LITEHTML_REV (first build only)"
    rm -rf "$SRC"
    git init -q "$SRC"
    git -C "$SRC" remote add origin "$LITEHTML_REPO"
    git -C "$SRC" fetch -q --depth 1 origin "$LITEHTML_REV"
    git -C "$SRC" checkout -q FETCH_HEAD
else
    have="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo none)"
    if [ "$have" != "$LITEHTML_REV" ]; then
        log "upstream tree at $have, moving to pinned $LITEHTML_REV"
        git -C "$SRC" fetch -q --depth 1 origin "$LITEHTML_REV"
        git -C "$SRC" checkout -q --detach FETCH_HEAD
    fi
fi
actual="$(git -C "$SRC" rev-parse HEAD)"
[ "$actual" = "$LITEHTML_REV" ] || die "upstream is at $actual, expected the pinned $LITEHTML_REV"

# ----------------------------------------------------------------- build
log "configuring litehtml (Release, testing off, lint off)"
cmake -S "$SRC" -B "$BUILD_DIR/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DLITEHTML_BUILD_TESTING=OFF \
    -DLITEHTML_ENABLE_LINT=OFF \
    -DEXTERNAL_GUMBO=OFF >/dev/null
log "compiling litehtml + bundled gumbo"
cmake --build "$BUILD_DIR/build" -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" >/dev/null

LITEHTML_LIB="$(find "$BUILD_DIR/build" -name 'liblitehtml.a' -print -quit)"
GUMBO_LIB="$(find "$BUILD_DIR/build" -name 'libgumbo.a' -print -quit)"
[ -n "$LITEHTML_LIB" ] || die "liblitehtml.a not produced"
[ -n "$GUMBO_LIB" ] || die "libgumbo.a not produced (bundled parser missing)"

log "compiling displayd_html shim against $CXX_BIN"
"$CXX_BIN" -O2 -std=c++17 -fPIC -fvisibility=hidden -Wall -Wextra \
    -I"$SRC/include" -I"$NATIVE" \
    -DLITEHTML_PINNED_REV="\"$LITEHTML_REV\"" \
    -shared \
    "$NATIVE/shim.cpp" "$NATIVE/pil_container.cpp" \
    "$LITEHTML_LIB" "$GUMBO_LIB" \
    -o "$LIB"

# ---------------------------------------------------------------- licence
# Apache-2.0 (bundled gumbo) requires its licence to travel with the binary.
# Refresh both from the pinned tree so they can never drift from the code.
log "refreshing licence notices from the pinned tree"
cp "$SRC/LICENSE" "$NATIVE/LICENSE-litehtml"
cp "$SRC/src/gumbo/LICENSE" "$NATIVE/LICENSE-gumbo"

log "built $LIB"
log "size: $(du -h "$LIB" | cut -f1)  licenses: LICENSE-litehtml (BSD-3-Clause), LICENSE-gumbo (Apache-2.0)"
