"""Drawn-vs-live touch region assertion: DRAWN renderer geometry vs the
LIVE region set announced by the running touch service (POST
/touch/announce). File-vs-file diffs miss this drift (files agree while
the service dispatches its startup set). Reachable as GET /touch/check
(matrix over every view, gated in deploy.sh). Per-view regions:
global entries live everywhere, view_regions.<view> only while that
view shows -- global sets cannot separate two view-specific areas
sharing screen space (picker tiles vs the macbook map). No input-loop
reload: freshness via unit restart.

The two halves live next door and are re-exported here, because the daemon
and the touch service import this module by name: the announce wire format
in touch_audit_wire.py and the per-view live/drawn region sets in
touch_audit_regions.py. What stays here is the comparison that gives the
audit its verdict.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from touch_audit_regions import (candidates, candidates_for_mode,
                                 expected_for_view, mode_live)
from touch_audit_wire import (announce_payload, normalize_live, regions_sha,
                              validate_announce)

COVERAGE = ("exact(id+rect): home/sleep badges; exact(+action): picker, "
            "unified, retro-grid cells; presence-only: macbook_mouse, "
            "sleep screen_on; reported-not-asserted: strips, "
            "reload_confirm, feedback; unasserted: layout, blank")


def compare_exact(expected_exact, live, force_strict=False):
    """Exact entries vs live. Undrawn live ids reported, never failed.
    A drawn id with no live region fails only when required (or the view
    is claimed via force_strict); unclaimed opt-in geometry (retro cells
    with no scope) lands in unwired instead of failing the matrix."""
    missing, moved, unwired = [], [], []
    for entry in expected_exact or []:
        got = live.get(entry["id"])
        if got is None:
            bucket = (missing if entry.get("required", True)
                      or force_strict else unwired)
            bucket.append({"id": entry["id"],
                           "expected_rect": entry["rect"],
                           "expected_action": entry["action"]})
            continue
        changed = (entry["action"] is not None
                   and got["action"] != entry["action"])
        if got["rect"] != entry["rect"] or changed:
            moved.append({"id": entry["id"],
                          "expected_rect": entry["rect"],
                          "live_rect": got["rect"],
                          "action_changed": changed,
                          "expected_action": entry["action"],
                          "live_action": got["action"]})
    wanted = {e["id"] for e in expected_exact or []}
    return {"ok": not missing and not moved, "missing": missing,
            "moved": moved, "unwired": unwired,
            "unasserted": sorted(set(live) - wanted)}


def compare_presence(presence, live):
    """Required actions vs live (rects are operator-chosen)."""
    have = {(info.get("action") or {}).get("name") for info in live.values()}
    missing = [p for p in presence or [] if p.get("action") not in have]
    return {"ok": not missing, "missing": missing}
