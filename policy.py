"""Policy layer for displayd: what the screen does on its own.

displayd's core is purely reactive -- it changes only when someone POSTs
/show. This module is the single place that decides otherwise. It owns:

- the activity clock (last API request, last feed delivery, last explicit
  selection) that all three autonomous behaviours read;
- the configuration surface for all three behaviours (validated, persisted
  to disk so it survives a restart);
- the switch-away/return mechanism shared by notifications and
  chat-attention ("temporarily show something else, then go back");
- the idle-off decision ("nothing happened for X, blank the panel").

Priority is explicit, not emergent: notice (2) > attention (1). A higher
priority transient preempts a lower one; a lower one arriving while a
higher is active is ignored. The return target is always the last explicit
(manual) selection -- never another transient -- so stacked transients can
never strand the panel. Any manual /show or /clear bumps the generation
counter, which cancels every in-flight transient: the manual choice wins,
and a late timer can never clobber it.

Idle power-off is the lowest priority of all: it fires only when no
transient is active, and any noted activity wakes the panel first.

This module draws nothing and touches no framebuffer; the daemon supplies
the actuators. Unit-testable with a fake clock.
"""

import copy
import json
import os
import threading
import time

# Priority: higher preempts lower. Idle-off is not a transient; it only
# fires when no transient is active.
PRIORITY = {"attention": 1, "notice": 2}

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
}


def default_config():
    return copy.deepcopy(DEFAULTS)


class Policy:
    """See module docstring. All state changes are lock-guarded."""

    def __init__(self, path=None, clock=None):
        self._lock = threading.Lock()
        self._clock = clock or time.time
        self.path = path
        self.config = default_config()
        now = self._clock()
        self.last_api = now
        self.last_feed = None
        self.last_select = None
        self.base = None  # last explicit selection {"renderer", "params"}
        self.active = None  # {"kind", "token", "deadline"} or None
        self.generation = 0
        self.idle_off = False
        if path:
            self.load()

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

    # ---- activity clock ------------------------------------------------

    def note_api(self):
        with self._lock:
            self.last_api = self._clock()

    def note_feed(self):
        with self._lock:
            now = self._clock()
            self.last_feed = now
            self.last_api = now

    def note_select(self, renderer, params):
        """A manual selection: new base view, every transient cancelled."""
        with self._lock:
            now = self._clock()
            self.last_select = now
            self.last_api = now
            self.base = {"renderer": renderer, "params": copy.deepcopy(params or {})}
            self.generation += 1
            self.active = None
            return self.generation

    def note_clear(self):
        with self._lock:
            now = self._clock()
            self.last_select = now
            self.last_api = now
            self.base = None
            self.generation += 1
            self.active = None

    def last_activity(self):
        with self._lock:
            marks = [m for m in (self.last_api, self.last_feed, self.last_select)
                     if m is not None]
        return max(marks) if marks else None

    def activity_snapshot(self):
        now = self._clock()
        with self._lock:
            snap = {}
            for name in ("last_api", "last_feed", "last_select"):
                mark = getattr(self, name)
                snap[name] = None if mark is None else round(now - mark, 1)
            return snap

    # ---- switch-away/return ---------------------------------------------

    def begin_transient(self, kind, duration):
        """Request a transient switch. Returns (token, superseded_kind) or
        (None, active_kind) when a higher-or-equal transient holds the
        screen. Equal priority preempts (re-arm): the newest event owns the
        return timer."""
        if kind not in PRIORITY:
            raise ValueError("unknown transient kind %r" % (kind,))
        with self._lock:
            if self.active is not None:
                incumbent = PRIORITY[self.active["kind"]]
                if incumbent > PRIORITY[kind]:
                    return None, self.active["kind"]
                superseded = self.active["kind"]
            else:
                superseded = None
            self.generation += 1
            token = self.generation
            self.active = {"kind": kind, "token": token,
                           "deadline": self._clock() + duration,
                           "return_to": copy.deepcopy(self.base)}
            return token, superseded

    def transient_valid(self, kind, token):
        with self._lock:
            return (self.active is not None
                    and self.active["kind"] == kind
                    and self.active["token"] == token)

    def end_transient(self, kind, token):
        """Fire the return timer: if nothing manual happened since, hand
        back the base view to restore (None clears to blank)."""
        with self._lock:
            if not (self.active is not None
                    and self.active["kind"] == kind
                    and self.active["token"] == token):
                return None, False
            self.active = None
            return copy.deepcopy(self.base), True

    def transient_status(self):
        now = self._clock()
        with self._lock:
            if self.active is None:
                return {"active": None, "generation": self.generation}
            out = dict(self.active)
            out["active"] = out.pop("kind")
            out["in_seconds"] = round(max(0.0, out.pop("deadline") - now), 1)
            out["generation"] = self.generation
            return out

    # ---- idle ------------------------------------------------------------

    def idle_due(self):
        """True when the panel should blank itself right now."""
        cfg = self.get_config()["idle"]
        if not cfg["enabled"] or self.idle_off:
            return False
        with self._lock:
            if self.active is not None:
                return False  # a transient is showing; its push was recent
            marks = [m for m in (self.last_api, self.last_feed, self.last_select)
                     if m is not None]
        if not marks:
            return False
        return (self._clock() - max(marks)) >= cfg["after_seconds"]
