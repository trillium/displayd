"""The panel draws its own columns.

Single concept: keeping the panel's regions in step with its ``columns``
(surface_columns.py). A column ``{stream, view, region}`` is drawn *by the
panel itself* in its region, with the html renderer and ``column.html``
(surface_column_view.py): no Sidecar and no content delivery involved.

Rules:

* only while a layout is active -- a column never hijacks a panel that is
  showing a single view; with no layout every column is simply waiting;
* a column whose region is not in the layout is *dormant* (surface_builtin_
  panel.py) and draws nothing until its region returns;
* repaints are coalesced: any number of events inside ``interval`` become one
  paint, and a region is rebound only when its drawn params actually changed
  (the cache is keyed by layout epoch, so a new layout repaints everything);
* each region goes through ``apply`` (the daemon's ``_rebind_region``), which
  replaces that region's renderer alone -- other regions are never disturbed;
* a failed region is recorded in ``status()`` and the rest still paint.

Painting is autonomous, so it never counts as operator activity. One content
delivery to a column's region replaces the column's drawing until the next
change to that column or its stream repaints it.
"""

import json
import threading
import time

import surface_column_view as view
from surface_columns import columns_of

PANEL_ID = "panel"
INTERVAL = 0.25


class ColumnPainter:
    def __init__(self, hub, regions, apply, epoch, interval=INTERVAL,
                 clock=time.time):
        """``regions()`` -> ``[(slug, layout name, rect)]`` (empty: no layout);
        ``apply(layout name, params)`` redraws one region; ``epoch()`` names
        the active layout, so a different one invalidates the cache."""
        self._hub, self._regions, self._apply = hub, regions, apply
        self._epoch, self._interval, self._clock = epoch, interval, clock
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._cache = {}
        self._status = {"painted": 0, "regions": {}, "errors": {}}
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="column-painter")
        hub.on_columns_changed(self.poke)
        self._thread.start()

    def poke(self):
        """Something a column shows may have changed; paint soon."""
        self._wake.set()

    def stop(self):
        self._stopped.set()
        self._wake.set()

    def _loop(self):
        while not self._stopped.is_set():
            self._wake.wait()
            if self._stopped.is_set():
                return
            self._stopped.wait(self._interval)  # coalesce a burst
            self._wake.clear()
            try:
                self.paint()
            except Exception as err:  # the loop outlives any one bad paint
                self._status["errors"]["*"] = "%s: %s" % (type(err).__name__, err)

    def paint(self):
        """One pass over the panel's columns; returns the regions repainted."""
        regions = {slug: (name, rect) for slug, name, rect in self._regions()}
        epoch = self._epoch()
        if not regions:
            self._cache.clear()
            self._status["regions"] = {}
            return []
        surface = self._hub.registry.get(PANEL_ID)
        painted, shown = [], {}
        for column in columns_of(surface):
            if column["region"] not in regions:
                continue
            name, rect = regions[column["region"]]
            stream = self._hub.streams.find(column["stream"])
            events = (self._hub.streams.read(column["stream"])["events"]
                      if stream else [])
            params = {"template": view.TEMPLATE, "vars": view.build_vars(
                column, stream, events, view.plan(rect))}
            key = (epoch, json.dumps(params, sort_keys=True))
            shown[column["region"]] = column["id"]
            if self._cache.get(name) == key:
                continue
            try:
                self._apply(name, params)
            except Exception as err:
                self._status["errors"][column["region"]] = "%s: %s" % (
                    type(err).__name__, err)
                continue
            self._cache[name] = key
            self._status["errors"].pop(column["region"], None)
            self._status["painted"] += 1
            painted.append(column["region"])
        self._status["regions"] = shown
        return painted

    def status(self):
        return {"painted": self._status["painted"],
                "regions": dict(self._status["regions"]),
                "errors": dict(self._status["errors"])}
