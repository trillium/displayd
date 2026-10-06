"""Renderer discovery: the renderers/ directory and its shared helpers.

Single concept: loading renderer modules (``load_renderers``) and the shared
renderers/ helpers the daemon itself reuses -- the component layer's system
buttons, macbook map and layout geometry, and talon apps. A broken plugin
becomes a marker entry and a missing helper becomes ``None``; neither ever
takes the daemon down.

This is also where a view's capability declaration is normalized: an
undeclared or misspelled one becomes ``full`` (see capability.py), so a
view that has not said it can be reduced cannot land in a layout slot --
and where its palette accent slot is resolved: the view's identity colour
lives in ``theme.ACCENT_SLOTS``, never as a per-view literal, and the entry
carries it so a plugin's own ``ACCENT`` declaration can still win.
"""

import importlib.util
import os

import capability

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
try:
    theme_module = _load_shared_helper("theme")
except Exception:
    theme_module = None


def _accent_slot(name):
    """The palette accent slot for a view name, or ``None``.

    ``None`` means the palette does not know this view (a plugin, a
    synthetic test entry), which is a legitimate answer: the playlist bar
    then falls through to the plugin's own declaration and the default.
    """
    if theme_module is None:
        return None
    try:
        return theme_module.accent(name, None)
    except Exception:  # a palette must never stop a renderer loading
        return None


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
        view_name = getattr(mod, "NAME", name)
        found[view_name] = {
            "module": mod,
            "description": getattr(mod, "DESCRIPTION", ""),
            "params": getattr(mod, "PARAMS", {}),
            "inputs": getattr(mod, "INPUTS", {}),
            "static": getattr(mod, "STATIC", True),
            "capability": capability.coerce(
                getattr(mod, "CAPABILITY", None)),
            "accent_slot": _accent_slot(view_name),
        }
    return found
