#!/bin/sh
# Point git at the committed hooks (.githooks/). Run once per clone.
cd "$(git rev-parse --show-toplevel)" || exit 1
git config core.hooksPath .githooks && echo "hooks installed: .githooks/"
