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

    def __init__(self, registry=None, **registry_kwargs):
        self.registry = registry or SurfaceRegistry(**registry_kwargs)
        self.streams = EventStreams(registry_kwargs.get("clock", time.time))
        self._renderers = {}
        self._relay = RelayRenderer()

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
