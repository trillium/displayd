"""The panel block of a surface: a stable identity and its config object.

Single concept: what stays true of a *logical panel* when the physical
surface changes. A surface record has ``panel: {"id", "version", "config"}``:

``id``
    a stable token naming the logical panel, distinct from the surface id
    (the surface is the transient connection; the panel is who it is). Unset
    (``None``) for an anonymous registration.
``config``
    one opaque, bounded JSON object holding everything per-panel: today the
    ``columns`` (surface_columns.py, validated against the regions); future
    per-panel settings (accessibility scale, renderer choice) are further keys
    of this same object, so they need no new field.
``version``
    the config schema version, so a later release can migrate a stored config.

Only the *shape* lives here. Recognising a returning panel by ``id`` and
restoring its config is pending (docs/SURFACES.md "Roadmap").
"""

import json

from surface_columns import normalize_columns
from surface_model import ID_RE, _dict, _token

CONFIG_VERSION = 1
MAX_CONFIG_BYTES = 16 * 1024


def normalize_panel(raw, regions):
    raw = _dict(raw or {}, "panel")
    pid = raw.get("id")
    config = dict(_dict(raw.get("config") or {}, "panel.config"))
    if "columns" in config:
        config["columns"] = normalize_columns(config["columns"], regions)
    else:
        config["columns"] = []
    if len(json.dumps(config)) > MAX_CONFIG_BYTES:
        raise ValueError("panel.config: over %d bytes" % MAX_CONFIG_BYTES)
    return {"id": None if pid is None else _token(pid, "panel.id", ID_RE),
            "version": CONFIG_VERSION, "config": config}
