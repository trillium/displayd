#!/usr/bin/env python3
"""Host touch.json transform for panel sleep/wake (displayd-screen-off-control).

Adds (preserving everything else byte-for-byte in spirit):
  regions: screen-off badge [1760,0,160,160] after home (before the
    full-height strips it overlaps -- hit_test takes the first match)
  view_regions.sleep: one fullscreen wake region (screen_on)

Usage on lnx-server (as trillium):
  cp ~/displayd/touch.json ~/displayd/backups/touch.json.pre-sleep-<date>
  python3 sleep_touch_transform.py ~/displayd/touch.json
  python3 ~/displayd/touch.py --config ~/displayd/touch.json --check-views
  sudo systemctl restart displayd-touch   # re-announces; deploy.sh does this
Revert: restore the backup and restart displayd-touch again.
"""
import copy
import json
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/trillium/displayd/touch.json"

SCREEN_OFF = {"id": "screen-off", "rect": [1760, 0, 160, 160],
              "action": {"name": "screen_off"}}
WAKE = {"id": "wake", "rect": [0, 0, 1920, 1080],
        "action": {"name": "screen_on"}}


def main():
    with open(PATH, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    regions = doc.get("regions") or []
    ids = [r.get("id") for r in regions]
    if "screen-off" not in ids:
        at = ids.index("home") + 1 if "home" in ids else 0
        regions.insert(at, copy.deepcopy(SCREEN_OFF))
        doc["regions"] = regions
    scoped = doc.get("view_regions") or {}
    if "sleep" not in scoped:
        scoped["sleep"] = [copy.deepcopy(WAKE)]
        doc["view_regions"] = scoped
    with open(PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    print("regions:", [r["id"] for r in doc["regions"]])
    print("scopes:", sorted(doc["view_regions"]))


if __name__ == "__main__":
    main()
