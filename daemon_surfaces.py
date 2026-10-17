"""The panel as a registered surface, and the on-device HTML implementation.

Single concept: wiring the renderer-agnostic surface layer (surface_*.py)
onto this daemon. Three things live here:

* the ``surface_hub`` (created lazily, so construction in daemon_core is
  untouched; ``main()`` touches it at boot) with the built-in ``panel``
  surface registered on it and its registry persisted (surface_store.py);
* the panel's self-description: viewport = the framebuffer, regions = the
  active layout's regions (or one ``full`` region), touch = ``tap`` only and
  only while the touch service's heartbeat is fresh (touch.py dispatches
  taps; it drops swipes and long presses by design);
* the ``displayd-html`` implementation's actuator: content for a panel
  region binds the ``html`` renderer into *that region only*
  (``_rebind_region``), leaving every other region's thread and frame alone.
"""

import os
import re
import threading
import time

from daemon_config import APP_VERSION
from schema import validate_params
from screen import RegionScreen
from surface_hub import SurfaceHub
from surface_model import DEFAULT_REGION, SurfaceError
from surface_renderers import HTML_CONTENT_TYPE, HTML_RENDERER, HtmlSurfaceRenderer
from surface_store import SurfaceStore

PANEL_ID = "panel"
HTML_VIEW = "html"  # the renderers/ module the displayd-html implementation drives


class _GuardedRegionScreen(RegionScreen):
    """A region screen whose late frames are dropped once it is replaced,
    so a rebind never lets the old renderer overpaint the new one."""

    def __init__(self, daemon, name, width, height, stop):
        RegionScreen.__init__(self, daemon, name, width, height)
        self._replaced = stop

    def present(self, img):
        if not self._replaced.is_set():
            RegionScreen.present(self, img)


def surfaces_path(policy_path):
    """Host-local registry file: ``DISPLAYD_SURFACES``, else ``state/`` next
    to the policy file (the daemon's directory on the host; a test's temp dir
    in a suite). ``deploy.sh`` neither ships nor deletes it."""
    return os.environ.get("DISPLAYD_SURFACES") or os.path.join(
        os.path.dirname(os.path.abspath(policy_path)), "state", "surfaces.json")


class SurfacesMixin:
    """Registered display surfaces for the daemon."""

    _surface_hub_lock = threading.Lock()

    @property
    def surface_hub(self):
        hub = self.__dict__.get("_surface_hub")
        if hub is None:
            with self._surface_hub_lock:
                hub = self.__dict__.get("_surface_hub")
                if hub is None:
                    hub = SurfaceHub(store=SurfaceStore(
                        surfaces_path(self.policy.path)))
                    hub.register_renderer(HtmlSurfaceRenderer(self._panel_apply))
                    hub.registry.register_builtin(PANEL_ID, self._panel_spec)
                    self._surface_hub = hub
        return hub

    def surfaces_state(self):
        """``GET /state``'s ``surfaces`` key; never builds the hub."""
        hub = self.__dict__.get("_surface_hub")
        return None if hub is None else hub.persistence()

    # ---- the panel's self-description ----------------------------------

    def _panel_touch(self):
        live = self.touch_live
        fresh = (live is not None and time.time() - live["announced_at"]
                 <= self.TOUCH_HEARTBEAT_MAX_AGE)
        return {"supported": fresh, "interactions": ["tap"] if fresh else [],
                "max_points": 1 if fresh else 0}

    def _panel_regions(self):
        """Active layout regions as ``(surface region name, layout name,
        rect)``. Layout names are free text; a surface region name is a
        token, so each is slugged (and de-duplicated) -- the same mapping
        names a region in the spec and finds it again on delivery."""
        with self.layout_lock:
            layout = [] if self.layout is None else list(self.layout)
        out, seen = [], set()
        for i, region in enumerate(layout):
            slug = re.sub(r"[^a-z0-9._+-]+", "-", region["name"].lower()).strip("-.+_")
            slug = slug[:56] or "region"
            if slug in seen:
                slug = "%s-%d" % (slug, i)
            seen.add(slug)
            out.append((slug, region["name"], region["rect"]))
        return out

    def _panel_spec(self):
        touch = self._panel_touch()
        regions = [{"name": slug, "rect": dict(zip("xywh", rect)),
                    "touch": touch["supported"]}
                   for slug, _, rect in self._panel_regions()] or None
        return {"name": "DisplayD panel", "kind": "panel",
                "renderer": {"name": HTML_RENDERER, "version": APP_VERSION,
                             "content_types": [HTML_CONTENT_TYPE]},
                "viewport": {"width": self.fb.width, "height": self.fb.height,
                             "scale": 1},
                "touch": touch, "regions": regions}

    # ---- the displayd-html actuator -------------------------------------

    def _panel_apply(self, region_name, params):
        """Draw html params into one panel region (or the whole panel when
        no layout is active). Raises SurfaceError when it cannot."""
        entry = self.renderers.get(HTML_VIEW)
        if not entry or "module" not in entry:
            raise SurfaceError(503, "the html renderer is not loaded")
        validate_params(params, entry.get("params") or {})
        self.policy.note_api()  # a mutating send is operator activity
        regions = {slug: name for slug, name, _ in self._panel_regions()}
        if not regions:
            if region_name != DEFAULT_REGION:
                raise SurfaceError(404, "panel has no region %r" % region_name)
            self.show(HTML_VIEW, params)
        elif region_name in regions:
            self._rebind_region(regions[region_name], HTML_VIEW, params)
        else:
            raise SurfaceError(404, "panel has no region %r" % region_name)

    def _rebind_region(self, name, renderer_name, params):
        """Replace ONE layout region's renderer; the others keep running.

        The old region thread is told to stop and its screen is guarded so
        a late frame cannot overpaint; the region keeps showing its last
        frame until the new renderer draws, so there is no flash of black.
        Raises SurfaceError for an unknown region or a full-panel-only view
        in a region smaller than the panel."""
        entry = self.renderers[renderer_name]
        with self.lock:
            with self.layout_lock:
                region = None if self.layout is None else next(
                    (r for r in self.layout if r["name"] == name), None)
                if region is None:
                    raise SurfaceError(404, "panel has no region %r" % name)
                x, y, w, h = region["rect"]
                if (w, h) != (self.fb.width, self.fb.height) and \
                        entry.get("capability") == "full":
                    raise SurfaceError(409, "%s cannot render in a %dx%d region"
                                       % (renderer_name, w, h))
                region["renderer"], region["params"] = renderer_name, params
                self.region_errors.pop(name, None)
            old = self.region_threads.get(name)
            if old is not None:
                old["stop"].set()
            stop = threading.Event()
            screen = _GuardedRegionScreen(self, name, w, h, stop)
            screen.current_view = renderer_name
            thread = threading.Thread(
                target=self._run_region,
                args=(entry, screen, params, stop, name), daemon=True)
            self.region_threads[name] = {
                "thread": thread, "stop": stop, "renderer": renderer_name,
                "params": params, "rect": region["rect"], "screen": screen}
            thread.start()
