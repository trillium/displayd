"""Single-view lifecycle: what is showing and how it changes.

Single concept: the shown view -- starting a renderer thread, the picker's
default live view list, clearing, stopping, and the manual show/clear entry
points (which hold playlist rotation and cancel transients).
"""

import threading
import time

from schema import validate_params


class ViewLifecycleMixin:
    """Single-view show/clear lifecycle."""

    # ---- content -------------------------------------------------------

    def _picker_live_params(self, entry, params):
        """Tile-grid default views: no explicit list means the live
        advertised set, so a new view appears with no config edit.
        Explicit lists win. The unified home screen draws every view
        but itself (a self tile would just re-show home). Never raises."""
        params = dict(params or {})
        mod = (entry or {}).get("module")
        name = getattr(mod, "NAME", "")
        if name in ("picker", "unified") and "views" not in params:
            try:
                if name == "unified":
                    params["views"] = mod.live_tile_views(self.renderers)
                else:
                    params["views"] = mod.live_views(self.renderers)
            except Exception:
                pass
        return params

    def _run(self, entry, params, stop):
        try:
            entry["module"].run(
                self.screen, self._picker_live_params(entry, params), stop)
        except Exception as err:
            self.last_error = "%s: %s" % (type(err).__name__, err)

    def _start_view(self, name, params):
        """Put a view on screen without touching policy state.

        Manual entries (show/clear) go through the public methods, which
        record the base view and cancel transients first. Transient entries
        and returns come here directly, so a timer firing can never rewrite
        the base view or defeat a manual selection."""
        entry = self.renderers.get(name)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % name)
        started = time.time()
        with self.lock:
            with self.cache_lock:
                cached = self.frame_cache.get(name)
            with self.screen.present_lock:
                self.screen.current_view = name
                if cached is not None:
                    # Stale frame beats a pause: memcpy the last known good
                    # frame now; the fresh draw follows on the new thread.
                    try:
                        self.fb.present(cached)
                    except Exception:
                        cached = None
            self._stop_locked()
            stop = threading.Event()
            self.stop_event = stop
            self.current = name
            self.current_params = dict(params or {})
            # Single funnel: every navigation voids a pending sleep
            # return, so a later power-on never yanks back a view the
            # operator already replaced (e.g. a manual /show while
            # dark). Only _enter_sleep_view re-arms the slot, after
            # the sleep switch lands.
            self.sleep_restore = None
            self.started_at = started
            self.last_error = None
            self.last_switch_at = started
            with self.cache_lock:
                if cached is not None:
                    self.last_switch_ms = round((time.time() - started) * 1000, 1)
                    self.switch_pending = started  # still time the fresh draw
                else:
                    self.last_switch_ms = None
                    self.switch_pending = started
            self.thread = threading.Thread(
                target=self._run, args=(entry, params or {}, stop), daemon=True
            )
            self.thread.start()
        return self.state()

    def _clear_internal(self):
        with self.lock:
            self._stop_locked()
            self.current = None
            self.current_params = None
            self.started_at = None
            self.screen.current_view = None
            # Blanking voids a pending sleep return with it (the funnel
            # above only covers _start_view; see layout()/clear_layout).
            self.sleep_restore = None
            with self.cache_lock:
                self.switch_pending = None
        self.screen.clear()
        return self.state()

    def show(self, name, params):
        entry = self.renderers.get(name)
        if not entry or "module" not in entry:
            raise KeyError("unknown renderer: %s" % name)
        # Validate before touching what is on screen: a bad selection is
        # rejected and the current view keeps running undisturbed.
        validate_params(params or {}, entry.get("params") or {})
        self.policy.note_select(name, params)
        self.playlist.on_manual()  # manual choice wins: hold the rotation
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
            if self.reload_token is not None:
                # Manual navigation cancels the confirmation window.
                self.reload_tokens.pop(self.reload_token, None)
                self.reload_token = None
        return self._start_view(name, params)

    def clear(self):
        self.policy.note_clear()
        self.playlist.on_manual()
        with self.lock:
            self._cancel_transient_timer()
            self._wake_if_idle()
            if self.reload_token is not None:
                self.reload_tokens.pop(self.reload_token, None)
                self.reload_token = None
        return self._clear_internal()

    def _stop_locked(self):
        """Stop everything drawing: the single view (if any) and every
        layout region (if any). A layout cleared here means late region
        frames are dropped by the generation check in _present_region.
        Callers hold self.lock."""
        if self.stop_event is not None:
            self.stop_event.set()
        self.thread = None
        self.stop_event = None
        for record in self.region_threads.values():
            try:
                record["stop"].set()
            except Exception:
                pass
        self.region_threads = {}
        with self.layout_lock:
            self.layout = None
            self.layout_preset = None
            self.region_frames = {}
            self.region_errors = {}
            self.region_updated = {}
            self.layout_pending = None
            self.layout_gen += 1
