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
import threading
import time

import surface_model as model
from surface_channels import Channels
from surface_panel import normalize_panel
from surface_target import match
from surface_touch import build_event

TOMBSTONES = 64
PATCH_MERGE = ("renderer", "viewport", "touch")  # one-level merge, rest replace


def _merge_panel(old, patch):
    """``id`` replaces; ``config`` merges one level (a key given replaces)."""
    out = dict(old, **{k: v for k, v in patch.items() if k != "config"})
    if isinstance(patch.get("config"), dict):
        out["config"] = dict(old.get("config") or {}, **patch["config"])
    elif "config" in patch:
        out["config"] = patch["config"]
    return out


class SurfaceRegistry:
    """Thread-safe registry with an injectable clock."""

    def __init__(self, clock=time.time, default_ttl=model.DEFAULT_TTL):
        self._clock = clock
        self._default_ttl = default_ttl
        self._lock = threading.RLock()
        self._live = {}       # id -> stored record
        self._builtin = {}    # id -> provider() -> raw spec
        self._tombs = collections.OrderedDict()  # id -> expired_at
        self.channels = Channels(clock)

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
            rec["panel"] = normalize_panel(spec.get("panel"), rec["regions"])
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
        rec["panel"] = normalize_panel(spec.get("panel"), rec["regions"])
        with self._lock:
            now = self._clock()
            self._sweep(now)
            if rec["id"] in self._builtin:
                raise model.SurfaceError(
                    409, "%r is a built-in surface id" % rec["id"])
            old = self._live.get(rec["id"])
            rec.update(revision=(old["revision"] + 1) if old else 1,
                       registered_at=old["registered_at"] if old else now,
                       updated_at=now, last_seen=now,
                       expires_at=now + rec["ttl_seconds"])
            self._live[rec["id"]] = rec
            self._tombs.pop(rec["id"], None)
            self.channels.keep_regions(rec["id"], [r["name"] for r in rec["regions"]])
            return self._view(rec, now), old is None

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
            return self._view(rec, now)

    def update(self, sid, patch):
        """Merge a partial spec and re-validate the whole; atomic."""
        if not isinstance(patch, dict):
            raise ValueError("update: expected an object")
        if "id" in patch and patch["id"] != sid:
            raise ValueError("id: cannot change a surface's id")
        with self._lock:
            old = self._live_record(sid)
            merged = {k: v for k, v in old.items()
                      if k in ("id", "name", "kind", "renderer", "viewport",
                               "touch", "regions", "labels", "ttl_seconds",
                               "panel")}
            regions_default = model.is_default_regions(old)
            for key, value in patch.items():
                turning_touch_off = (key == "touch" and isinstance(value, dict)
                                     and value.get("supported") is False)
                if key == "panel" and isinstance(value, dict):
                    merged[key] = _merge_panel(merged[key], value)
                elif (key in PATCH_MERGE and isinstance(value, dict)
                        and not turning_touch_off):
                    merged[key] = dict(merged[key], **value)
                else:
                    merged[key] = value
            if "regions" not in patch and regions_default:
                merged.pop("regions")  # re-derive: geometry/types may have moved
            rec = model.normalize(merged, default_ttl=self._default_ttl)
            rec["panel"] = normalize_panel(merged["panel"], rec["regions"])
            now = self._clock()
            rec.update(revision=old["revision"] + 1,
                       registered_at=old["registered_at"], updated_at=now,
                       last_seen=now, expires_at=now + rec["ttl_seconds"])
            self._live[sid] = rec
            self.channels.keep_regions(sid, [r["name"] for r in rec["regions"]])
            return self._view(rec, now)

    def deregister(self, sid):
        with self._lock:
            self._live_record(sid)
            del self._live[sid]
            self._tombs.pop(sid, None)  # a deliberate leave is not an expiry
            self.channels.drop(sid)
            return {"ok": True, "surface": sid}

    # ---- touch ---------------------------------------------------------

    def touch(self, sid, body):
        """Record one reported touch interaction; returns the event."""
        surface = self.get(sid)
        if surface["builtin"]:
            raise model.SurfaceError(
                409, "the panel's touch is dispatched on-device by touch.py, "
                     "not reported through the registry")
        return self.channels.add_event(sid, build_event(surface, body))
