"""Static-region composition: several renderers on one panel.

Single concept: the opt-in layout mode on top of the single-view core -- one
thread and cached last-good frame per region, presents recomposited from the
per-region cache, contained renderer crashes, and the layout state the API
reports. Geometry parsing is layout.py; the named styles built on it (and
the views each slot may carry) are layout_presets.py.
"""

import threading
import time

from PIL import Image
import layout_presets as presets
from layout import parse_layout
from screen import RegionScreen


class LayoutMixin:
    """Static-region composition lifecycle."""

    # ---- static-region composition (ISA D6 option B) --------------------

    def _run_region(self, entry, screen, params, stop, region_name):
        """One region's draw loop. A crash is contained (ISA D7): the
        error is recorded for /state and the region keeps its
        last-good-frame -- other regions never notice."""
        try:
            entry["module"].run(
                screen, self._picker_live_params(entry, params), stop)
        except Exception as err:
            with self.layout_lock:
                if (self.layout is not None and
                        any(r["name"] == region_name for r in self.layout)):
                    self.region_errors[region_name] = "%s: %s" % (
                        type(err).__name__, err)

    def _overlaid(self, base):
        """Apply the shared overlay chain to a layout composite.

        The system buttons (and the playlist bar) are always-on furniture
        composited by ``Screen.overlay`` for a single view; a layout
        composite is presented straight to the framebuffer, so it has to
        pass through the same chain or the badges vanish the moment a
        layout owns the panel -- while their touch regions stay live.
        Never raises: a broken overlay layer is skipped."""
        overlay = getattr(self.screen, "overlay", None)
        if overlay is None:
            return base
        try:
            return overlay(base)
        except Exception:
            return base

    def _present_region(self, region_name, img):
        """Cache one region's frame and recomposite the panel from the
        per-region cache. Never holds layout_lock while presenting, so a
        slow framebuffer write can never stall another region's update."""
        try:
            frame = img.copy()
        except Exception as err:
            with self.layout_lock:
                self.region_errors[region_name] = "%s: %s" % (
                    type(err).__name__, err)
            return
        with self.layout_lock:
            if self.layout is None:
                return
            match = [r for r in self.layout if r["name"] == region_name]
            if not match:
                return
            rect = match[0]["rect"]
            gen = self.layout_gen
            try:
                if frame.size != (rect[2], rect[3]):
                    frame = frame.resize((rect[2], rect[3]))
                frame = frame.convert("RGB")
            except Exception as err:
                self.region_errors[region_name] = "%s: %s" % (
                    type(err).__name__, err)
                return
            self.region_frames[region_name] = frame
            self.region_updated[region_name] = time.time()
            # A fresh frame clears the region's error: the renderer is
            # visibly alive again, so /state should say so.
            self.region_errors.pop(region_name, None)
            base = Image.new("RGB", (self.fb.width, self.fb.height),
                               (0, 0, 0))
            for region in self.layout:
                cached = self.region_frames.get(region["name"])
                if cached is None:
                    continue
                try:
                    base.paste(cached, (region["rect"][0],
                                        region["rect"][1]))
                except Exception:
                    continue  # one bad frame never breaks the composite
        with self.screen.present_lock:
            with self.layout_lock:
                if gen != self.layout_gen or self.layout is None:
                    return  # superseded by a mode change; drop it
            try:
                self.fb.present(self._overlaid(base))
            except Exception:
                pass
        with self.layout_lock:
            if self.layout_pending is not None and gen == self.layout_gen:
                self.layout_switch_ms = round(
                    (time.time() - self.layout_pending) * 1000, 1)
                self.layout_pending = None

    def _composite_now(self):
        """Present the current per-region cache immediately (black for
        regions that have not drawn yet), so a layout switch paints its
        first pixel without waiting on any renderer."""
        with self.layout_lock:
            if self.layout is None:
                return
            gen = self.layout_gen
            base = Image.new("RGB", (self.fb.width, self.fb.height),
                               (0, 0, 0))
            for region in self.layout:
                cached = self.region_frames.get(region["name"])
                if cached is None:
                    continue
                try:
                    base.paste(cached, (region["rect"][0],
                                        region["rect"][1]))
                except Exception:
                    continue
        with self.screen.present_lock:
            with self.layout_lock:
                if gen != self.layout_gen or self.layout is None:
                    return
            try:
                self.fb.present(self._overlaid(base))
            except Exception:
                pass
        with self.layout_lock:
            if self.layout_pending is not None and gen == self.layout_gen:
                self.layout_switch_ms = round(
                    (time.time() - self.layout_pending) * 1000, 1)
                self.layout_pending = None

    def set_layout(self, payload):
        """Activate a static-region layout, replacing the single view.

        A request may name one of the presets instead of its regions
        (``{"preset": "15-70-15", "views": {...}}``); the preset is
        expanded into the same region grammar, so one parser stays the
        only authority on geometry and capability fit.

        Validation (unknown renderer, bad params, bad geometry, a
        full-panel-only view in a reduced region) happens first via
        parse_layout: a bad layout is rejected and whatever is on screen
        keeps running undisturbed. A bare POST /show exits layout mode and
        returns to single-renderer behaviour."""
        preset = payload.get("preset") if isinstance(payload, dict) else None
        if preset:
            payload = presets.build(payload, self.renderers)
        regions = parse_layout(payload, self.fb.width, self.fb.height,
                               self.renderers)
        started = time.time()
        self.policy.note_api()  # mutating POST: activity, but the base
        # view is untouched -- transients cannot interrupt a layout.
        self.playlist.on_manual()  # an explicit layout wins: hold rotation
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
            self._stop_locked()
            self.sleep_restore = None  # a layout owns the panel now
            with self.layout_lock:
                self.layout = regions
                self.layout_preset = preset
                self.layout_started_at = started
                self.layout_pending = started
                self.layout_switch_ms = None
                for region in regions:
                    x, y, w, h = region["rect"]
                    self.region_frames[region["name"]] = Image.new(
                        "RGB", (w, h), (0, 0, 0))
            self.current = None
            self.current_params = None
            self.started_at = None
            self.screen.current_view = "layout"
            for region in regions:
                x, y, w, h = region["rect"]
                screen = RegionScreen(self, region["name"], w, h)
                screen.current_view = region["renderer"]
                stop = threading.Event()
                entry = self.renderers[region["renderer"]]
                thread = threading.Thread(
                    target=self._run_region,
                    args=(entry, screen, region["params"], stop,
                          region["name"]),
                    daemon=True)
                self.region_threads[region["name"]] = {
                    "thread": thread, "stop": stop,
                    "renderer": region["renderer"],
                    "params": region["params"], "rect": region["rect"],
                    "screen": screen,
                }
                thread.start()
            with self.cache_lock:
                self.last_switch_at = started
        self._composite_now()
        return self.state()

    def clear_layout(self):
        """Drop the layout and blank the screen (DELETE /layout)."""
        self.policy.note_clear()
        self.playlist.on_manual()
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
            self._stop_locked()
            self.current = None
            self.current_params = None
            self.started_at = None
            self.screen.current_view = None
            self.sleep_restore = None  # blank owns the panel now
            with self.cache_lock:
                self.switch_pending = None
        self.screen.clear()
        return self.state()

    def layout_state(self):
        """The layout fragment of /state: None when inactive."""
        with self.layout_lock:
            if self.layout is None:
                return None
            regions = []
            for region in self.layout:
                x, y, w, h = region["rect"]
                regions.append({
                    "name": region["name"],
                    "renderer": region["renderer"],
                    "params": region["params"],
                    "rect": {"x": x, "y": y, "w": w, "h": h},
                    "error": self.region_errors.get(region["name"]),
                    "updated_at": self.region_updated.get(region["name"]),
                })
            return {
                "preset": self.layout_preset,
                "regions": regions,
                "started_at": self.layout_started_at,
                "first_pixel_ms": self.layout_switch_ms,
            }
