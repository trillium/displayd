#!/usr/bin/env python3
"""Regenerate the picker + unified tile scopes of a host touch.json.

Usage (on the panel host, from the deployed tree):
  python3 tools/regen_touch_tiles.py --config touch.json            # dry run
  python3 tools/regen_touch_tiles.py --config touch.json --apply    # write

When the renderer set changes (a view ships, the html runtime is installed)
the drawn picker/unified grids gain a tile and every later tile shifts, so
the host's tile rects tap the WRONG view until they are regenerated. This is
the TOUCH.md "Reference wiring" procedure as one command: the view list is
the live advertised set (same rule as the daemon's `live_views()`: no
required params, not broken, sorted; the unified scope drops `unified`
itself) and the rects come from `picker_regions()` / `unified_regions()`,
the functions the renderers draw from -- never hand-computed.

Only `view_regions.picker` and `view_regions.unified` are touched, and only
if the scope already exists. Dry run exits 1 when they have drifted, 0 when
in sync. `--apply` backs the file up first (`backups/touch.json.pre-tiles-
<date>` next to the config), validates the result with `load_config()`
BEFORE replacing the file, and never restarts anything: restart
`displayd-touch` and re-check `GET /touch/check` yourself.
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "renderers")]

from renderers import picker, unified  # noqa: E402
import touch_config  # noqa: E402

DEFAULT_ENDPOINT = "http://100.81.88.113:8980"


def live_set(doc):
    """Tile views from a GET /renderers document (daemon `live_views()` rule)."""
    def gated(entry):
        return any(isinstance(s, dict) and s.get("required")
                   for s in (entry.get("params") or {}).values())
    return sorted(r["name"] for r in doc.get("renderers", [])
                  if "broken" not in r and not gated(r))


def regenerate(cfg, views):
    """New {scope: entries} for each existing picker/unified scope."""
    w, h = int(cfg.get("width", 1920)), int(cfg.get("height", 1080))
    scopes = cfg.get("view_regions") or {}
    out = {}
    if "picker" in scopes:
        out["picker"] = picker.picker_regions(w, h, views)
    if "unified" in scopes:
        # entry [0] is the global sleep badge; the daemon draws home from
        # the live set minus `unified` itself.
        out["unified"] = unified.unified_regions(
            w, h, [v for v in views if v != "unified"])[1:]
    return out


def tile_ids(entries):
    return [e["id"] for e in entries]


def describe(old, new):
    gone = sorted(set(tile_ids(old)) - set(tile_ids(new)))
    came = sorted(set(tile_ids(new)) - set(tile_ids(old)))
    moved = sum(1 for o in old for n in new
                if o["id"] == n["id"] and o["rect"] != n["rect"])
    return "added %s, removed %s, moved %d" % (came, gone, moved)


def write_atomic(path, cfg):
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    try:
        touch_config.load_config(tmp)
    except Exception:
        os.unlink(tmp)
        raise
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="host touch.json")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                    help="displayd base URL for GET /renderers")
    ap.add_argument("--apply", action="store_true",
                    help="back up and rewrite (default: dry run)")
    args = ap.parse_args(argv)

    with open(args.config, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    with urllib.request.urlopen(args.endpoint.rstrip("/") + "/renderers",
                                timeout=10) as resp:
        views = live_set(json.load(resp))

    scopes = cfg.get("view_regions") or {}
    new = regenerate(cfg, views)
    drift = {k: v for k, v in new.items() if v != scopes[k]}
    for name in new:
        state = describe(scopes[name], new[name]) if name in drift else "in sync"
        print("%s: %s" % (name, state))
    if not drift:
        return 0
    if not args.apply:
        print("drift found; re-run with --apply to back up and rewrite")
        return 1

    backups = os.path.join(os.path.dirname(os.path.abspath(args.config)),
                           "backups")
    os.makedirs(backups, exist_ok=True)
    backup = os.path.join(backups, "touch.json.pre-tiles-%s"
                          % datetime.date.today().strftime("%Y%m%d"))
    if os.path.exists(backup):
        print("refusing to overwrite existing backup %s" % backup)
        return 2
    shutil.copy2(args.config, backup)
    cfg["view_regions"].update(drift)
    write_atomic(args.config, cfg)
    print("wrote %s (backup %s)" % (args.config, backup))
    print("next: restart displayd-touch, then GET /touch/check must be ok:true")
    print("revert: cp -p %s %s && restart displayd-touch" % (backup, args.config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
