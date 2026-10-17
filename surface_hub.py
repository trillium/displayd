"""The surface hub: the one door consumers use to reach a surface.

Single concept: registry + renderer implementations + delivery (the event
``streams`` that columns show hang off the hub too, but are independent of
every surface; see surface_streams.py). A consumer
hands the hub a target (see surface_target.py) and a content object; the hub
resolves it to ``(surface, region)`` pairs, checks the content type is one the
region accepts, lets the surface's renderer implementation draw it, and
stores it in the region's slot (so a pulling Sidecar and a late joiner both
see the latest).

Content is ``{"type": "<content type>", ...}`` -- the ``type`` is matched
against the surface/region ``content_types``; everything else belongs to the
renderer implementation. Size is bounded so one send cannot flood memory.

A send to one named surface fails loudly (404/409/410/415/400). A *broadcast*
(``select`` with ``all``) reports per-surface ``delivered`` and ``skipped``
instead, because one Sidecar being the wrong shape must not hide the others.
"""

import json
import time

from surface_model import SurfaceError
from surface_registry import SurfaceRegistry
from surface_streams import EventStreams
from surface_renderers import RelayRenderer
from surface_restore import restore_streams
from surface_store import seq_base
from surface_target import parse_target, resolve

MAX_CONTENT_BYTES = 256 * 1024


def check_content(content):
    if not isinstance(content, dict):
        raise ValueError("content: expected an object")
    if not isinstance(content.get("type"), str) or not content["type"]:
        raise ValueError("content.type: required")
    try:
        size = len(json.dumps(content))
    except (TypeError, ValueError):
        raise ValueError("content: not JSON-serializable")
    if size > MAX_CONTENT_BYTES:
        raise ValueError("content: %d bytes over the %d cap"
                         % (size, MAX_CONTENT_BYTES))
    return content


class SurfaceHub:
    """Resolve, validate, render, store."""

    def __init__(self, registry=None, store=None, **registry_kwargs):
        """``store`` (a SurfaceStore) makes registered surfaces, their columns
        and the stream declarations survive a restart: its file is loaded
        here (stale leases become tombstones) and rewritten on every change."""
        self.store, self.restored = store, None
        doc = store.load() if store else None
        base = seq_base(store.generation) if store else 0
        change = store.changed if store else None
        self.registry = registry or SurfaceRegistry(
            on_change=change, seq_base=base, **registry_kwargs)
        self.streams = EventStreams(registry_kwargs.get("clock", time.time),
                                    seq_base=base, on_change=change)
        self._renderers = {}
        self._relay = RelayRenderer()
        if store:
            self.restored = self._restore(doc)
            store.attach(self._snapshot)
            store.save()  # commits this boot's generation before any event

    def _restore(self, doc):
        report = self.registry.restore(doc["surfaces"], doc["tombs"])
        decls, bad = restore_streams(doc["streams"])
        self.streams.restore(decls)
        panels, panels_bad = self.registry.panels.restore(doc.get("panels"))
        return dict(report, streams=len(decls), streams_invalid=bad,
                    panels=panels, panels_invalid=panels_bad)

    def on_columns_changed(self, fn):
        """``fn()`` whenever something a column shows may have changed: an
        event published, a stream declared/removed, a panel block set."""
        self.streams.subscribe(fn)
        self.registry.panels.subscribe(lambda sid: fn())

    def persistence(self):
        """The store's status plus what the boot restored, or None."""
        return None if self.store is None else dict(self.store.status(),
                                                    restored=self.restored)

    def _snapshot(self):
        return dict(self.registry.export(), streams=self.streams.export())

    def register_renderer(self, impl):
        """Install a renderer implementation under its ``name``."""
        self._renderers[impl.name] = impl

    def renderer_for(self, surface):
        return self._renderers.get(surface["renderer"]["name"], self._relay)

    def _deliver_one(self, surface, region, content):
        if content["type"] not in region["content_types"]:
            raise SurfaceError(
                415, "region %s/%s accepts %s, not %r"
                % (surface["id"], region["name"],
                   ", ".join(region["content_types"]), content["type"]))
        impl = self.renderer_for(surface)
        impl.validate(content)
        outcome = impl.render(surface, region, content)
        seq = self.registry.channels.put_content(
            surface["id"], region["name"], content)
        return dict(outcome, surface=surface["id"], region=region["name"],
                    seq=seq, renderer=surface["renderer"]["name"])

    def deliver(self, target, content):
        """``{"delivered": [...], "skipped": [...]}``; raises on a single
        named target that cannot take the content."""
        check_content(content)
        broadcast = parse_target(target)["all"]
        delivered, skipped = [], []
        for surface, region in resolve(self.registry, target, content["type"]):
            try:
                delivered.append(self._deliver_one(surface, region, content))
            except (SurfaceError, ValueError) as err:
                if not broadcast:
                    raise
                skipped.append({"surface": surface["id"], "region": region["name"],
                                "reason": str(err)})
        if broadcast and not delivered:
            raise SurfaceError(415, "no matched surface accepted the content: %s"
                               % "; ".join(s["reason"] for s in skipped))
        return {"delivered": delivered, "skipped": skipped}
