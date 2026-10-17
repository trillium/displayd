"""The panel block of a built-in surface: settable, persisted, layout-proof.

Single concept: a built-in surface (the device panel) is re-described from a
provider on every read, so it has no stored record to hold a ``panel`` block
-- this holds one per built-in id. Two rules make it safe on a device whose
regions move:

* what is *stored* is validated when it is set (against the regions the
  surface has at that moment), and survives a layout change;
* what is *shown* is only the columns whose region exists right now. The
  rest are **dormant**: kept, listed in ``panel.dormant``, drawn again when
  their region returns. A stale column therefore never breaks discovery.

Setting replaces a config key wholesale (``surface_patch.merge_panel``), so
``columns`` is edited by sending the list that should exist.
"""

import copy
import threading

from surface_model import SurfaceError
from surface_panel import normalize_panel
from surface_patch import merge_panel

EMPTY = {"id": None, "config": {}}


def split_columns(panel, region_names):
    """``(panel with only the active columns, [dormant column ids])``."""
    cols = (panel.get("config") or {}).get("columns") or []
    live = [c for c in cols if c.get("region") in region_names]
    dormant = [c["id"] for c in cols if c.get("region") not in region_names]
    return dict(panel, config=dict(panel["config"], columns=live)), dormant


class BuiltinPanels:
    """Stored panel blocks by built-in surface id; thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._panels = {}
        self._listeners = []

    def subscribe(self, fn):
        """``fn(surface_id)`` after a panel block changed (no lock held)."""
        self._listeners.append(fn)

    def view(self, sid, regions):
        """The normalized panel for a built-in's record, dormant noted."""
        with self._lock:
            stored = copy.deepcopy(self._panels.get(sid, EMPTY))
        active, dormant = split_columns(stored, [r["name"] for r in regions])
        return dict(normalize_panel(active, regions), dormant=dormant)

    def update(self, sid, patch, regions):
        """Apply a surface patch (only ``panel`` may be set on a built-in);
        ``ValueError`` when invalid, nothing stored."""
        if set(patch) - {"panel", "id"}:
            raise SurfaceError(409, "%r is a built-in surface: only its panel is settable" % sid)
        if not isinstance(patch.get("panel"), dict):
            raise ValueError("a built-in surface only accepts a 'panel' update")
        with self._lock:
            merged = merge_panel(copy.deepcopy(self._panels.get(sid, EMPTY)),
                                 patch["panel"])
            kept = normalize_panel(merged, regions)  # raises on a bad column
            self._panels[sid] = {"id": kept["id"], "config": kept["config"]}
        for fn in list(self._listeners):
            fn(sid)

    def export(self):
        with self._lock:
            return copy.deepcopy(self._panels)

    def restore(self, stored):
        """Boot-time load; ``(count, invalid)``. Columns are checked for
        shape only -- their regions may not exist until a layout does."""
        ok, bad = {}, 0
        for sid, panel in (stored or {}).items():
            try:
                cols = ((panel.get("config") or {}).get("columns")) or []
                loose = [{"name": c["region"]} for c in cols]
                kept = normalize_panel(panel, loose)
                ok[str(sid)] = {"id": kept["id"], "config": kept["config"]}
            except (ValueError, KeyError, AttributeError, TypeError):
                bad += 1
        with self._lock:
            self._panels.update(ok)
        return len(ok), bad
