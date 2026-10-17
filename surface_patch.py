"""Merging a partial surface update into a stored spec.

Single concept: how ``POST /surfaces/<id>`` patches combine with what is
stored, before the whole result is re-validated by ``surface_model.normalize``
and ``surface_panel.normalize_panel``. Pure functions over plain data.
"""

import surface_model as model

PATCH_MERGE = ("renderer", "viewport", "touch")  # one-level merge, rest replace
SPEC_KEYS = ("id", "name", "kind", "renderer", "viewport", "touch", "regions",
             "labels", "ttl_seconds", "panel")


def merge_panel(old, patch):
    """``id`` replaces; ``config`` merges one level (a key given replaces)."""
    out = dict(old, **{k: v for k, v in patch.items() if k != "config"})
    if isinstance(patch.get("config"), dict):
        out["config"] = dict(old.get("config") or {}, **patch["config"])
    elif "config" in patch:
        out["config"] = patch["config"]
    return out


def merge_patch(old, patch):
    """The raw spec ``patch`` produces from the stored record ``old``."""
    merged = {k: v for k, v in old.items() if k in SPEC_KEYS}
    regions_default = model.is_default_regions(old)
    for key, value in patch.items():
        turning_touch_off = (key == "touch" and isinstance(value, dict)
                             and value.get("supported") is False)
        if key == "panel" and isinstance(value, dict):
            merged[key] = merge_panel(merged[key], value)
        elif (key in PATCH_MERGE and isinstance(value, dict)
                and not turning_touch_off):
            merged[key] = dict(merged[key], **value)
        else:
            merged[key] = value
    if "regions" not in patch and regions_default:
        merged.pop("regions")  # re-derive: geometry/types may have moved
    return merged
