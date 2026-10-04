#!/usr/bin/env python3
"""Fail if generated native artifacts are tracked in git."""

import subprocess
import sys

FORBIDDEN = [
    'renderers/native/liblitehtmlpil.dylib',
    'renderers/native/liblitehtmlpil.so',
]

def main():
    try:
        tracked = subprocess.check_output(
            ['git', 'ls-files', '--cached', '--full-name', '-z'],
            text=False,
        )
    except subprocess.CalledProcessError as e:
        print(f"git ls-files failed: {e}", file=sys.stderr)
        return 2
    seen = []
    for p in tracked.split(b'\x00'):
        if not p:
            continue
        s = p.decode('utf-8', errors='replace')
        for f in FORBIDDEN:
            if s == f:
                seen.append(s)
    if seen:
        for s in seen:
            print(f"tracked generated artifact: {s}")
        return 1
    print("ok: no generated native artifacts tracked")
    return 0

if __name__ == '__main__':
    sys.exit(main())
