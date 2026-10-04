#!/usr/bin/env python3
"""Run repository health checks deterministically."""

import subprocess
import sys

def run(cmd):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return p.returncode, p.stdout
    except Exception as e:
        return 1, str(e)

def main():
    ok = True
    # 1. Line budget
    rc, out = run([sys.executable, 'tools/check-lines.py'])
    if rc != 0:
        ok = False
        print(out)
    else:
        print(out.rstrip())
    # 2. No tracked generated artifacts
    rc, out = run([sys.executable, 'tools/check-generated-tracked.py'])
    if rc != 0:
        ok = False
        print(out)
    else:
        print(out.rstrip())
    if not ok:
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
