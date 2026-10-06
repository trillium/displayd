#!/usr/bin/env python3
"""Test double for the ffmpeg binary (tests/fixtures, never shipped).

Records the argv it was handed as one JSON array per line in the file
named by ``$FAKE_FFMPEG_LOG``, then exits 0. It captures nothing and
encodes nothing: it exists so a test can run a command line the bridges
built through a real ``exec`` and read back exactly what reached the
process, on any host, without assuming where a real ffmpeg lives.

Not a stand-in for ffmpeg's behaviour -- only for its place in argv[0].
"""

import json
import os
import sys


def main():
    log = os.environ.get("FAKE_FFMPEG_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sys.argv) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
