"""The surface registry: register, heartbeat/expiry, update, deregister.

Single concept: which surfaces exist right now. Two kinds:

* *registered* surfaces (a Sidecar announcing itself) carry a TTL. They stay
  live only while they heartbeat; an expired one is removed on the next read
  or write and remembered briefly as a tombstone, so its owner can tell
  "expired, re-register" (410) from "never existed" (404).
* *built-in* surfaces (the panel) come from a provider callable, never
  expire, and are re-described on every read, so their geometry and regions
  follow the device (a layout change reshapes the panel's regions).

Nothing here knows any renderer: a surface's ``renderer`` block is data.
Delivery is surface_hub.py; touch/content streams are surface_channels.py.
"""

import collections
import copy
import threading
import time

import surface_model as model
from surface_builtin_panel import BuiltinPanels
from surface_channels import Channels
from surface_identity import Identities
from surface_panel import normalize_panel
from surface_patch import merge_patch
from surface_restore import restore_surfaces
from surface_target import match
from surface_touch import build_event

TOMBSTONES = 64


class SurfaceRegistry:
    """Thread-safe registry with an injectable clock."""

    def __init__(self, clock=time.time, default_ttl=model.DEFAULT_TTL,
                 on_change=None, seq_base=0):
        self._clock = clock
        self._default_ttl = default_ttl
        self._on_change = on_change or (lambda urgent=True: None)
        self._lock = threading.RLock()
        self._live = {}       # id -> stored record
        self._builtin = {}    # id -> provider() -> raw spec
        self._tombs = collections.OrderedDict()  # id -> expired_at
        self.channels = Channels(clock, seq_base)
        self.panels = BuiltinPanels()  # settable panel blocks of the built-ins
        self.identities = Identities()  # remembered panel configs (surface_identity.py)

    # ---- views ---------------------------------------------------------

    def _view(self, rec, now):
        out = dict(rec)
        if rec["expires_at"] is not None:
            out["expires_in"] = round(max(0.0, rec["expires_at"] - now), 3)
        return out

    def _sweep(self, now):
        for sid in [s for s, r in self._live.items() if r["expires_at"] <= now]:
            del self._live[sid]
            self.channels.drop(sid)
            self._tombs[sid] = now
            while len(self._tombs) > TOMBSTONES:
                self._tombs.popitem(last=False)

    def _builtin_record(self, sid):
        try:
            spec = dict(self._builtin[sid](), id=sid)
            rec = model.normalize(spec, builtin=True)
            rec["panel"] = self.panels.view(sid, rec["regions"])
        except Exception as err:  # a broken provider must not break discovery
            raise model.SurfaceError(503, "built-in surface %r unavailable: %s"
                                     % (sid, err))
        rec.update(revision=None, registered_at=None, updated_at=None,
                   last_seen=None, expires_at=None)
        return rec

    def register_builtin(self, sid, provider):
        """Install a never-expiring surface described by ``provider()``."""
        with self._lock:
            self._builtin[sid] = provider
            self._live.pop(sid, None)  # a stored record never shadows a built-in

    # ---- reads ---------------------------------------------------------

    def get(self, sid):
        with self._lock:
            now = self._clock()
            self._sweep(now)
            if sid in self._builtin:
                return self._builtin_record(sid)
            if sid in self._live:
                return self._view(self._live[sid], now)
            if sid in self._tombs:
                raise model.SurfaceError(
                    410, "surface %r expired (no heartbeat): register again" % sid)
            raise model.SurfaceError(404, "unknown surface %r" % sid)

    def list(self, query=None):
        """Live surfaces (built-ins first by id, then registered by id)
        that satisfy the optional select query."""
        with self._lock:
            now = self._clock()
            self._sweep(now)
            found = []
            for sid in sorted(self._builtin):
                try:
                    found.append(self._builtin_record(sid))
                except model.SurfaceError:
                    continue
            found.extend(self._view(self._live[s], now) for s in sorted(self._live))
            return [s for s in found if match(s, query)]

    # ---- lifecycle -----------------------------------------------------

    def register(self, spec):
        """Idempotent upsert by id; returns ``(record, created)``."""
        rec = model.normalize(spec, default_ttl=self._default_ttl)
        panel = normalize_panel(spec.get("panel"), rec["regions"])
        with self._lock:
            now = self._clock()
            self._sweep(now)
            if rec["id"] in self._builtin:
                raise model.SurfaceError(
                    409, "%r is a built-in surface id" % rec["id"])
            panel, identity, restored = self.identities.adopt(
                panel, spec.get("panel"), rec["regions"], rec["id"], now)
            rec["panel"] = panel
            old = self._live.get(rec["id"])
            rec.update(revision=(old["revision"] + 1) if old else 1,
                       registered_at=old["registered_at"] if old else now,
                       updated_at=now, last_seen=now,
                       expires_at=now + rec["ttl_seconds"])
            self._live[rec["id"]] = rec
            self._tombs.pop(rec["id"], None)
            self.channels.keep_regions(rec["id"], [r["name"] for r in rec["regions"]])
            view = self._view(rec, now)
            # how the panel was recognised is told once, to the registrant
            view["panel"] = dict(panel, identity=identity,
                                 **({"restored": restored} if restored else {}))
            out = view, old is None
        self._on_change()  # outside the lock: persisting reads the registry
        return out

    def _live_record(self, sid):
        """The stored record, or the right refusal for why there is none."""
        self._sweep(self._clock())
        if sid in self._builtin:
            raise model.SurfaceError(409, "%r is a built-in surface" % sid)
        if sid in self._live:
            return self._live[sid]
        if sid in self._tombs:
            raise model.SurfaceError(
                410, "surface %r expired (no heartbeat): register again" % sid)
        raise model.SurfaceError(404, "unknown surface %r" % sid)

    def heartbeat(self, sid):
        """Extend the lease by the surface's own TTL."""
        with self._lock:
            rec = self._live_record(sid)
            now = self._clock()
            rec["last_seen"] = now
            rec["expires_at"] = now + rec["ttl_seconds"]
            out = self._view(rec, now)
        self._on_change(urgent=False)  # coalesced: a lost refresh is a 410
        return out

    def update(self, sid, patch):
        """Merge a partial spec and re-validate the whole; atomic."""
        if not isinstance(patch, dict):
            raise ValueError("update: expected an object")
        if "id" in patch and patch["id"] != sid:
            raise ValueError("id: cannot change a surface's id")
        if sid in self._builtin:
            with self._lock:
                regions = self._builtin_record(sid)["regions"]
            self.panels.update(sid, patch, regions)  # outside our lock
            self._on_change()
            return self.get(sid)
        with self._lock:
            old = self._live_record(sid)
            merged = merge_patch(old, patch)
            rec = model.normalize(merged, default_ttl=self._default_ttl)
            rec["panel"] = normalize_panel(merged["panel"], rec["regions"])
            now = self._clock()
            self.identities.remember(rec["panel"], sid, now)
            rec.update(revision=old["revision"] + 1,
                       registered_at=old["registered_at"], updated_at=now,
                       last_seen=now, expires_at=now + rec["ttl_seconds"])
            self._live[sid] = rec
            self.channels.keep_regions(sid, [r["name"] for r in rec["regions"]])
            out = self._view(rec, now)
        self._on_change()
        return out

    def deregister(self, sid):
        with self._lock:
            self._live_record(sid)
            del self._live[sid]
            self._tombs.pop(sid, None)  # a deliberate leave is not an expiry
            self.channels.drop(sid)
        self._on_change()
        return {"ok": True, "surface": sid}

    # ---- persistence (surface_store.py) --------------------------------

    def export(self):
        """Plain-data copy of what must survive a restart."""
        with self._lock:
            return {"surfaces": [copy.deepcopy(self._live[s]) for s in sorted(self._live)],
                    "tombs": dict(self._tombs), "panels": self.panels.export(),
                    "identities": self.identities.export()}

    def restore(self, records, tombs=None):
        """Load stored records at boot; returns the report (restored /
        expired / invalid counts). Stale leases become tombstones."""
        with self._lock:
            live, dead, report = restore_surfaces(
                records, tombs, self._clock(), self._default_ttl,
                reserved=tuple(self._builtin))
            self._live.update(live)
            for sid in live:
                self._tombs.pop(sid, None)
            self._tombs.update(dead)
            self._tombs = collections.OrderedDict(
                sorted(self._tombs.items(), key=lambda kv: kv[1])[-TOMBSTONES:])
            return report

    # ---- touch ---------------------------------------------------------

    def touch(self, sid, body):
        """Record one reported touch interaction; returns the event."""
        surface = self.get(sid)
        if surface["builtin"]:
            raise model.SurfaceError(
                409, "the panel's touch is dispatched on-device by touch.py, "
                     "not reported through the registry")
        return self.channels.add_event(sid, build_event(surface, body))
