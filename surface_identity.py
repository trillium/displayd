"""Panel identity: recognise a returning panel and restore its config.

Single concept: the memory behind ``panel.id`` (surface_panel.py). A logical
panel is not its physical surface, so when a registration names a ``panel.id``
displayd has seen before and brings no config of its own, the remembered config
(columns, accessibility scale, ...) is restored onto the new surface instead of
treating it as an unknown panel. Pure bookkeeping over plain data, no clock of
its own beyond the one handed in.

What the register response reports in ``panel.identity`` (told once, to the
registrant; not stored, so a later read does not repeat it):

``anonymous``  no ``panel.id``: nothing remembered, nothing restored;
``new``        an id displayd has not seen: registers as given (empty config is
               not remembered; the first explicit config is);
``kept``       the caller sent its own config: it wins and is remembered;
``restored``   a known id with no config: the remembered one is restored;
               ``panel.restored`` says from which surface and which columns
               were ``dropped`` because this surface has no such region;
``unusable``   a known id whose remembered config cannot be used (written by a
               newer build, no migration, or invalid here): the surface registers
               empty and the stored config is left untouched.

A restore never overwrites the memory (a smaller surface must not shrink what
a bigger one configured). Configs of an older ``version`` walk up through
``surface_panel.MIGRATIONS``.
"""

import copy
import threading

from surface_columns import normalize_columns
from surface_model import ID_RE
from surface_panel import CONFIG_VERSION, MIGRATIONS, normalize_panel

MAX_IDENTITIES = 256


def migrate_config(config, version):
    """``config`` walked from ``version`` up to ``CONFIG_VERSION``; ValueError."""
    if version > CONFIG_VERSION:
        raise ValueError("config version %d is newer than this build (%d)"
                         % (version, CONFIG_VERSION))
    while version < CONFIG_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise ValueError("no config migration from version %d" % version)
        config, version = step(config), version + 1
    return config


class Identities:
    """Remembered panel configs by ``panel.id``; thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._seen = {}  # panel id -> {"version", "config", "surface", "at"}

    def remember(self, panel, surface_id, now):
        """Keep ``panel``'s config under its id (no-op: anonymous or empty)."""
        config = panel.get("config") or {}
        if not panel.get("id") or not any(
                v for k, v in config.items() if k != "columns" or v):
            return
        with self._lock:
            self._seen.pop(panel["id"], None)  # re-insert: newest last
            self._seen[panel["id"]] = {"version": panel["version"],
                                       "config": copy.deepcopy(config),
                                       "surface": surface_id, "at": now}
            while len(self._seen) > MAX_IDENTITIES:
                self._seen.pop(next(iter(self._seen)))

    def adopt(self, panel, raw, regions, surface_id, now):
        """The panel a registration gets: ``panel`` itself, or the remembered
        config restored into it. Returns ``(panel, identity, restored|None)``."""
        pid = panel.get("id")
        if not pid:
            return panel, "anonymous", None
        if (raw or {}).get("config"):
            self.remember(panel, surface_id, now)
            return panel, "kept", None
        with self._lock:
            stored = copy.deepcopy(self._seen.get(pid))
        if stored is None:
            return panel, "new", None
        try:
            config = migrate_config(stored["config"], stored["version"])
            cols = config.get("columns") or []
            fits = [c for c in cols if c.get("region") in {r["name"] for r in regions}]
            config = dict(config, columns=normalize_columns(fits, regions))
            out = normalize_panel({"id": pid, "config": config}, regions)
        except (ValueError, KeyError, TypeError, AttributeError):
            return panel, "unusable", None
        kept = {c["id"] for c in out["config"]["columns"]}
        dropped = [c["id"] for c in cols if c.get("id") not in kept]
        return out, "restored", {"from_surface": stored["surface"],
                                 "dropped": dropped, "at": stored["at"]}

    def export(self):
        with self._lock:
            return copy.deepcopy(self._seen)

    def restore(self, stored):
        """Boot-time load; ``(count, invalid)``. Shape-checked only: the
        regions a config fits are judged when a surface registers."""
        ok, bad = {}, 0
        for pid, rec in (stored or {}).items():
            try:
                if not isinstance(pid, str) or not ID_RE.match(pid):
                    raise ValueError(pid)
                version, config = rec["version"], rec["config"]
                if isinstance(version, bool) or not isinstance(version, int) \
                        or not isinstance(config, dict):
                    raise ValueError(pid)
                ok[pid] = {"version": version, "config": config,
                           "surface": str(rec.get("surface") or ""),
                           "at": float(rec.get("at") or 0)}
            except (ValueError, KeyError, TypeError, AttributeError):
                bad += 1
        with self._lock:
            self._seen.update(ok)
        return len(ok), bad
