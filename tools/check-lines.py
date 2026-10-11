#!/usr/bin/env python3
"""Fail when a non-test source file exceeds the 250-line budget.

Usage: python3 tools/check-lines.py   (run from the repo root; no options)

Large files are a code smell: split by concept into single-concept modules.

Scope: every tracked code file (.py .sh .c .cpp .h .js .html) outside
tests/. The only exemption is VENDORED, a closed list of third-party files.

Files that were already over budget when the gate landed are pinned in
tools/line-limit-baseline.txt as "<cap> <path>". That is a ratchet, not a
waiver: a pinned file may not grow past its cap, must have its cap lowered
as it shrinks, and must leave the baseline once it is within budget.
Nothing new may be added over the limit.
"""

import pathlib
import subprocess
import sys

LIMIT = 250
CODE_SUFFIXES = {".py", ".sh", ".c", ".cpp", ".h", ".js", ".html"}
VENDORED = {"renderers/_qrcodegen.py"}  # Project Nayuki, MIT, unmodified
BASELINE = "tools/line-limit-baseline.txt"
DIRECTIVE = "split it by concept into single-concept modules of <= %d lines" % LIMIT


def candidates():
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=True).stdout.decode()
        paths = [pathlib.Path(p) for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        paths = [p for p in pathlib.Path(".").rglob("*") if ".git" not in p.parts]
    return sorted(p for p in paths if p.is_file())


def load_baseline():
    caps = {}
    path = pathlib.Path(BASELINE)
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.split("#")[0].strip()
            if line:
                cap, rel = line.split(None, 1)
                caps[rel] = int(cap)
    return caps


def main():
    caps = load_baseline()
    problems = []
    seen = set()
    for path in candidates():
        rel = path.as_posix()
        if path.suffix not in CODE_SUFFIXES or "tests" in path.parts[:-1]:
            continue
        if rel in VENDORED:
            continue
        count = len(path.read_text(errors="replace").splitlines())
        if rel in caps:
            seen.add(rel)
            cap = caps[rel]
            if count <= LIMIT:
                problems.append("%s is now %d lines: delete it from %s" % (rel, count, BASELINE))
            elif count > cap:
                problems.append("%s grew to %d lines (pinned at %d): %s" % (rel, count, cap, DIRECTIVE))
            elif count < cap:
                problems.append("%s shrank to %d lines: lower its cap in %s from %d" % (rel, count, BASELINE, cap))
        elif count > LIMIT:
            problems.append("%s is %d lines (limit %d): %s" % (rel, count, LIMIT, DIRECTIVE))
    for rel in sorted(set(caps) - seen):
        problems.append("%s is pinned in %s but no longer exists: delete the entry" % (rel, BASELINE))
    if problems:
        print("\n".join(problems))
        print("%d line-budget problem(s)" % len(problems))
        return 1
    print("line budget ok: all source files within %d lines (%d pinned)" % (LIMIT, len(caps)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
