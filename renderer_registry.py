"""Renderer discovery: the renderers/ directory and its shared helpers.

Single concept: loading renderer modules (``load_renderers``) and the shared
renderers/ helpers the daemon itself reuses -- the component layer's system
buttons, macbook map and layout geometry, and talon apps. A broken plugin
becomes a marker entry and a missing helper becomes ``None``; neither ever
takes the daemon down.
"""

import importlib.util
import os

RENDERER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "renderers")


def _load_shared_helper(name):
    """Load a shared renderers/ helper for daemon-side reuse.

    ``name`` is a path under renderers/ without the extension; a slashed
    subpath reaches into a package (``"ui/system_buttons"`` names a
    component-layer module). Helpers have no run(), so the renderer loader
    skips them -- but the daemon itself can still use them (the component
    layer's overlay chain). Raises like any import: callers that must
    survive a broken helper catch it (a missing badge must never take the
    daemon down)."""
    rel = name.replace(".", "/").strip("/")
    path = os.path.join(RENDERER_DIR, *rel.split("/")) + ".py"
    spec = importlib.util.spec_from_file_location(
        "displayd_" + rel.replace("/", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    system_buttons_module = _load_shared_helper("ui/system_buttons")
except Exception:
    system_buttons_module = None
try:
    macbook_map_module = _load_shared_helper("macbook_map")
except Exception:
    macbook_map_module = None
try:
    talon_apps_module = _load_shared_helper("talon_apps")
except Exception:
    talon_apps_module = None
# (The old side-column geometry, talon_layout, is still used by
# talon_apps.groups for the unified dock summary; the merged macbook
# header maps taps through macbook_layout instead.)
try:
    macbook_layout_module = _load_shared_helper("macbook_layout")
except Exception:
    macbook_layout_module = None


def load_renderers(directory):
    """Every module in renderers/ becomes a renderer. Drop a file in, it appears."""
    found = {}
    if not os.path.isdir(directory):
        return found
    for entry in sorted(os.listdir(directory)):
        if not entry.endswith(".py") or entry.startswith("_") or entry.startswith("."):
            continue
        path = os.path.join(directory, entry)
        name = entry[:-3]
        try:
            spec = importlib.util.spec_from_file_location("displayd_renderer_" + name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as err:  # a broken plugin must not take the daemon down
            found[name] = {"broken": str(err)}
            continue
        if not hasattr(mod, "run"):
            continue
        found[getattr(mod, "NAME", name)] = {
            "module": mod,
            "description": getattr(mod, "DESCRIPTION", ""),
            "params": getattr(mod, "PARAMS", {}),
            "inputs": getattr(mod, "INPUTS", {}),
            "static": getattr(mod, "STATIC", True),
        }
    return found
