#!/usr/bin/env python3
"""Fail when a source file exceeds the project's line budget.

Usage: python3 tools/check-lines.py [--limit N]

Budget: 250 lines per Python file. Excluded: tests/ (specs, not
shipped code) and vendored third-party modules (renderers/_*).
Run from the repo root; exit non-zero listing offenders.
"""

import pathlib
import sys

LIMIT = 250
EXCLUDE_DIRS = {"tests"}
EXCLUDE_PREFIXES = ("renderers/_",)


def main(argv):
    limit = int(argv[1]) if len(argv) > 1 else LIMIT
    root = pathlib.Path(".")
    bad = []
    for path in sorted(root.rglob("*.py")):
        rel = path.as_posix()
        if ".git" in path.parts:
            continue
        if path.parts[0] in EXCLUDE_DIRS:
            continue
        if rel.startswith(EXCLUDE_PREFIXES):
            continue
        count = len(path.read_text().splitlines())
        if count > limit:
            bad.append((count, rel))
    if bad:
        for count, rel in sorted(bad, reverse=True):
            print("%d %s" % (count, rel))
        print("%d file(s) over the %d-line budget" % (len(bad), limit))
        return 1
    print("line budget ok: all source files within %d lines" % limit)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
