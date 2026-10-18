"""Durable registry state: one versioned file, atomic writes, safe loads.

Single concept: what survives a daemon restart. The file is host-local state
(``state/surfaces.json`` next to the daemon, ``DISPLAYD_SURFACES`` overrides),
which ``deploy.sh`` never ships or deletes. It holds the *registry* only:

``surfaces``
    every registered (non-built-in) surface record, columns included (they
    live in ``panel.config``), with its absolute lease ``expires_at``.
``tombs``
    ``{id: expired_at}`` so a restart keeps answering 410 (expired,
    re-register) instead of 404 (never existed).
``streams``
    the declared event streams (declarations only).
``panels``
    ``{built-in id: {id, config}}``: the settable panel block (columns) of a
    built-in surface such as the device panel. Additive within format 1: a
    file without it loads as "none set".
``identities``
    ``{panel id: {version, config, surface, at}}``: the config each stable
    ``panel.id`` last had, so a returning panel is recognised
    (surface_identity.py). Additive within format 1.

Not stored, on purpose: event/action rings, touch events and content slots.
They are transient and bounded; a producer republishes. To keep a consumer's
``since=<seq>`` honest anyway, every restart is a new ``generation`` and all
counters start at ``generation << 32``, so a seq never repeats or regresses.

Rules:

* writes are atomic (temp file in the same directory, fsync, ``os.replace``)
  and never raise: a failed write is reported in ``status()`` and the API
  carries on;
* the file is versioned (``format``); an older file is walked up through
  ``MIGRATIONS`` (``{from_version: fn(doc) -> doc}``). A corrupt file or one
  written by a *newer* format is moved aside to ``<path>.unreadable-<n>``
  (never overwritten, never deleted) and the registry starts empty;
* stale leases are judged by the registry on restore (see
  ``SurfaceRegistry.restore``), so a surface whose lease ran out while the
  daemon was down becomes a tombstone, not a ghost;
* heartbeats only *request* a write (``changed(urgent=False)``): they are
  coalesced to one per ``flush_interval``, because losing a few seconds of
  lease refresh just means an early 410 and a re-register. Structural changes
  (register/update/deregister, stream declare/remove) write immediately.
"""

import json
import os
import sys
import threading
import time

FORMAT = 1
MIGRATIONS = {}  # from_version -> fn(doc) -> doc at from_version + 1
GENERATION_SHIFT = 32
FLUSH_INTERVAL = 5.0


def empty_doc():
    return {"format": FORMAT, "generation": -1, "surfaces": [], "tombs": {},
            "streams": [], "panels": {}, "identities": {}}


def seq_base(generation):
    return max(0, generation) << GENERATION_SHIFT


def migrate(doc):
    """Walk ``doc`` up to ``FORMAT``; ``ValueError`` when it cannot."""
    if not isinstance(doc, dict) or isinstance(doc.get("format"), bool) \
            or not isinstance(doc.get("format"), int):
        raise ValueError("no integer 'format'")
    while doc["format"] < FORMAT:
        step = MIGRATIONS.get(doc["format"])
        if step is None:
            raise ValueError("no migration from format %d" % doc["format"])
        doc = step(doc)
    if doc["format"] > FORMAT:
        raise ValueError("format %d is newer than this build (%d)"
                         % (doc["format"], FORMAT))
    return doc


class SurfaceStore:
    """The file, plus the write policy. Thread-safe."""

    def __init__(self, path, clock=time.time, flush_interval=FLUSH_INTERVAL):
        self.path = path
        self._clock = clock
        self._flush_interval = flush_interval
        self._write_lock = threading.Lock()
        self._snapshot = None
        self._last_write = None
        self._dirty = False
        self.generation = 0
        self.saved_at = None
        self.last_error = None
        self.loaded = {"file": False, "moved_aside": None, "error": None}

    # ---- load ----------------------------------------------------------

    def _aside(self, why):
        n = 0
        while os.path.exists("%s.unreadable-%d" % (self.path, n)):
            n += 1
        target = "%s.unreadable-%d" % (self.path, n)
        try:
            os.replace(self.path, target)
        except OSError as err:
            self.last_error = "could not move %s aside: %s" % (self.path, err)
            return None
        print("displayd: %s unreadable (%s); kept as %s" % (self.path, why, target),
              file=sys.stderr)
        return target

    def load(self):
        """The stored doc (migrated), or an empty one. Never raises; sets
        ``generation`` to the new boot's generation (uncommitted until the
        first write)."""
        doc = empty_doc()
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            raw = None
        except (OSError, ValueError) as err:
            self.loaded.update(error=str(err), moved_aside=self._aside(err))
            raw = None
        if raw is not None:
            try:
                doc = migrate(raw)
                self.loaded["file"] = True
            except ValueError as err:
                self.loaded.update(error=str(err), moved_aside=self._aside(err))
        # the generation of even an unreadable file still counts: a seq
        # handed out under it must not be handed out again
        gen = (raw if isinstance(raw, dict) else doc).get("generation")
        prior = gen if isinstance(gen, int) and not isinstance(gen, bool) else -1
        self.generation = max(prior, -1) + 1
        for key in ("tombs", "panels", "identities"):
            doc[key] = doc[key] if isinstance(doc.get(key), dict) else {}
        for key in ("surfaces", "streams"):
            doc[key] = doc[key] if isinstance(doc.get(key), list) else []
        return doc

    # ---- save ----------------------------------------------------------

    def attach(self, snapshot):
        """``snapshot()`` -> ``{"surfaces", "tombs", "streams", "panels"}`` (plain data)."""
        self._snapshot = snapshot

    def changed(self, urgent=True):
        """Registry state moved: write now (urgent) or when due."""
        due = self._last_write is None or \
            self._clock() - self._last_write >= self._flush_interval
        if urgent or due:
            self.save()
        else:
            self._dirty = True

    def flush(self):
        """Write if a coalesced change is still waiting."""
        if self._dirty:
            self.save()

    def save(self):
        """Atomic write of the current snapshot; True on success."""
        if self._snapshot is None:
            return False
        tmp = "%s.tmp-%d" % (self.path, os.getpid())
        with self._write_lock:  # snapshot inside the lock: writes stay ordered
            doc = dict(self._snapshot(), format=FORMAT, generation=self.generation,
                       saved_at=self._clock())
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, indent=1, sort_keys=True)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except (OSError, TypeError, ValueError) as err:
                self.last_error = str(err)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return False
            self.saved_at, self.last_error = doc["saved_at"], None
            self._last_write, self._dirty = self._clock(), False
            return True

    def status(self):
        return {"path": self.path, "format": FORMAT, "generation": self.generation,
                "saved_at": self.saved_at, "last_error": self.last_error,
                "pending_write": self._dirty, "loaded": dict(self.loaded)}
