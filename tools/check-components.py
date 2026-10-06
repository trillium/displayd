#!/usr/bin/env python3
"""Structural gate: the panel's pixels are drawn in the component layer.

Rule
====
`renderers/ui/` (the component layer) is the only place in the shipped UI
that may touch a Pillow drawing primitive. Concretely: a module in this
gate's scope that mentions ``ImageDraw`` is drawing by hand, and a view
that draws by hand is the duplication the component layer exists to
delete. Migrate it into a component.

Scope
=====
- every ``*.py`` at the repo root -- the daemon-side panel UI, where the
  playlist progress bar lives; and
- every ``*.py`` under ``renderers/``, recursively, except the component
  layer itself (``renderers/ui/``).

``tests/``, ``tools/``, ``bridges/``, ``hooks/`` and build output are out
of scope: they are not shipped panel pixels.

The ratchet
===========
The migration is in flight, so the rule is enforced as a ratchet.
``EXEMPTIONS`` lists exactly the files that still draw by hand, and the
gate fails in BOTH directions:

- a drawing file that is NOT exempt -- fail: new hand-drawing code, name
  it and migrate it instead;
- an exempt file that is gone, moved, or no longer mentions ``ImageDraw``
  -- fail: a stale exemption would hide a finished migration, and a
  renamed file has to re-earn its exemption.

So the list can only ever shrink. ``--list`` prints the current offenders
(sorted) to paste into ``EXEMPTIONS`` after a migration.

Deterministic: discovery is sorted, the comparison is set-based, and the
report is sorted. Exits non-zero with the offending files named.
"""

import argparse
import pathlib
import re
import sys

LAYER_DIR = ("renderers", "ui")
DRAW_RE = re.compile(r"\bImageDraw\b")


# Not migrated yet: every shipped module that still draws by hand. Each
# entry is a debt repaid by moving its drawing into a component; the gate
# fails if one of these files stops drawing (or stops existing) so the
# list cannot rot.
EXEMPTIONS = (
    "renderers/_html_native.py",
    "renderers/beads.py",
    "renderers/beads_detail_card.py",
    "renderers/life.py",
    "renderers/macbook_draw.py",
    "renderers/macbook_strip.py",
    "renderers/qr_common.py",
    "renderers/retro_grid_draw.py",
)


def repo_root():
    """The repo this tool ships in (tools/check-components.py)."""
    return pathlib.Path(__file__).resolve().parent.parent


def drawing_files(root):
    """Scoped modules that mention ImageDraw, as sorted relative POSIX
    paths. The component layer is excluded: drawing is its job."""
    layer = root.joinpath(*LAYER_DIR)
    renderers = root / "renderers"
    candidates = list(root.glob("*.py"))
    if renderers.is_dir():
        candidates += list(renderers.rglob("*.py"))
    found = set()
    for path in candidates:
        if "__pycache__" in path.parts:
            continue
        try:
            path.relative_to(layer)
            continue  # the component layer may draw
        except ValueError:
            pass
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if DRAW_RE.search(text):
            found.add(path.relative_to(root).as_posix())
    return sorted(found)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Fail while a shipped module draws outside the "
                    "component layer (renderers/ui/).")
    ap.add_argument("--root", default=None,
                    help="repo root to inspect (default: this repo)")
    ap.add_argument("--list", action="store_true",
                    help="print the current offenders, sorted, and exit 0 "
                         "(paste them into EXEMPTIONS after a migration)")
    args = ap.parse_args(argv)
    root = pathlib.Path(args.root).resolve() if args.root else repo_root()
    found = drawing_files(root)
    if args.list:
        for rel in found:
            print(rel)
        return 0
    exempt = set(EXEMPTIONS)
    new = sorted(set(found) - exempt)
    stale = sorted(exempt - set(found))
    if new or stale:
        for rel in new:
            print("outside the component layer, draws with ImageDraw: %s"
                  % rel)
        for rel in stale:
            print("stale exemption (gone, moved, or no longer draws): %s"
                  % rel)
        print("component gate: %d new, %d stale, %d still to migrate"
              % (len(new), len(stale), len(set(found) & exempt)))
        return 1
    print("component layer ok: %d shipped module(s) still draw by hand; "
          "all exempt, none stale" % len(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
