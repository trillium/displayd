"""Configuration surface for the displayd policy layer.

Single concept: the validated, persisted config that all three autonomous
behaviours read. Owns the defaults, the schema, deep-merge patching, and
load/save against the JSON file on disk, exposed as PolicyConfigMixin so
policy.Policy keeps one class with its runtime half (activity clock,
switch-away/return, idle-off) living in policy.py. Validation of the
playlist section delegates to playlist.py, the same as before.
"""

import copy
import json
import os

import playlist as playlist_module

DEFAULTS = {
    "notifications": {
        "enabled": True,
        "default_duration": 10,  # seconds a notice stays up when unset
    },
    "chat_attention": {
        "enabled": False,  # OFF by default: an option, not always-on
        "view": "chat",  # renderer a chat event pulls the panel to
        "input": "message",  # feed input name that counts as "a chat event"
        "return_after": 30,  # seconds on the chat view per event (re-armed)
    },
    "idle": {
        "enabled": False,  # OFF by default: blanking the panel unasked
        "after_seconds": 600,  # inactivity window before power-off
    },
    "playlist": {
        "enabled": False,  # OFF by default: rotation is opt-in
        "placement": "bottom",  # top | left | bottom | right
        "thickness": 10,  # bar thickness in px at panel resolution
        "direction": "fill",  # fill (empty->full) or drain (full->empty)
        "color": "#FFFFFF",  # default bar colour; per-view color wins,
        # then renderer ACCENT, then this (see playlist.accent_for)
        "tick_seconds": 0.2,  # overlay repaint cadence for parked views
        "views": [],  # [{renderer, params?, dwell?, color?}]
    },
}

# (section, key): (kind, min, max) for numbers; kind is "bool", "num", "str".
_SCHEMA = {
    ("notifications", "enabled"): ("bool", None, None),
    ("notifications", "default_duration"): ("num", 1, 300),
    ("chat_attention", "enabled"): ("bool", None, None),
    ("chat_attention", "view"): ("str", None, None),
    ("chat_attention", "input"): ("str", None, None),
    ("chat_attention", "return_after"): ("num", 5, 600),
    ("idle", "enabled"): ("bool", None, None),
    ("idle", "after_seconds"): ("num", 5, 86400),
    ("playlist", "enabled"): ("bool", None, None),
    ("playlist", "placement"): ("str", None, None),
    ("playlist", "thickness"): ("num", 2, 64),
    ("playlist", "direction"): ("str", None, None),
    ("playlist", "color"): ("str", None, None),
    ("playlist", "tick_seconds"): ("num", 0.05, 2.0),
}


def default_config():
    return copy.deepcopy(DEFAULTS)


class PolicyConfigMixin:
    """Validated, persisted config surface. Mixed into policy.Policy."""

    # ---- persistence -------------------------------------------------

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return False
        try:
            self._merge(raw)
        except ValueError:
            return False
        return True

    def save(self):
        if not self.path:
            return False
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.config, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
        return True

    # ---- configuration surface ----------------------------------------

    def get_config(self):
        with self._lock:
            return copy.deepcopy(self.config)

    def update_config(self, patch):
        """Deep-merge a patch, validate, persist. Raises ValueError."""
        with self._lock:
            merged = copy.deepcopy(self.config)
            self._apply(merged, patch)
            self._validate_all(merged)
            self.config = merged
            ok = self.save()
        return copy.deepcopy(self.config), ok

    def _merge(self, patch):
        merged = copy.deepcopy(self.config)
        self._apply(merged, patch)
        self._validate_all(merged)
        with self._lock:
            self.config = merged

    @staticmethod
    def _apply(merged, patch):
        if not isinstance(patch, dict):
            raise ValueError("policy patch must be an object")
        for section, values in patch.items():
            if section not in merged:
                raise ValueError("unknown policy section %r" % (section,))
            if not isinstance(values, dict):
                raise ValueError("policy section %r must be an object" % (section,))
            for key in values:
                if key not in merged[section]:
                    raise ValueError("unknown policy key %r.%s" % (section, key))
            merged[section].update(values)

    @staticmethod
    def _validate_all(config):
        for (section, key), (kind, lo, hi) in _SCHEMA.items():
            value = config[section][key]
            if kind == "bool":
                if not isinstance(value, bool):
                    raise ValueError("%s.%s must be boolean" % (section, key))
            elif kind == "num":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError("%s.%s must be a number" % (section, key))
                if not (lo <= value <= hi):
                    raise ValueError("%s.%s must be within [%s, %s]"
                                     % (section, key, lo, hi))
            elif kind == "str":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("%s.%s must be a non-empty string" % (section, key))
        playlist = config.get("playlist") or {}
        if playlist.get("placement") not in playlist_module.PLACEMENTS:
            raise ValueError("playlist.placement must be one of %s"
                             % "/".join(playlist_module.PLACEMENTS))
        if playlist.get("direction") not in playlist_module.DIRECTIONS:
            raise ValueError("playlist.direction must be one of %s"
                             % "/".join(playlist_module.DIRECTIONS))
        if playlist_module.parse_color(playlist.get("color"), None) is None:
            raise ValueError("playlist.color is not a colour")
        playlist_module.validate_views(playlist.get("views") or [])
