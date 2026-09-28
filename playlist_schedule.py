"""Playlist advancement: showing views and the scheduler loop.

Single concept: moving the rotation forward. Mixed into
:class:`playlist.Playlist`; state attributes and the read model
(``_config``, ``_views``, ``_hold_reason``) live on that class via
:class:`playlist_state.PlaylistStateMixin`.

Rotation yields: while a notice/attention transient holds the screen,
while the panel is blanked, while a static-region layout owns the panel,
or after an explicit manual ``/show``/``/clear``, the scheduler holds --
it does not advance, and the bar is hidden. Advancing never touches the
activity clock, so rotation cannot defeat idle-off. Broken views don't
stall: an unknown renderer is skipped after a short error dwell, and a
renderer that raises mid-dwell still advances on time.
"""

import time

ERROR_DWELL = 5.0  # seconds to linger (bar hidden) on an unshowable view
HISTORY = 50


class PlaylistScheduleMixin:
    """View advancement for the playlist scheduler (the moving part)."""

    def _show_current(self, item, dwell):
        name = item.get("renderer")
        params = item.get("params") or {}
        try:
            self.daemon.policy.note_playlist(name, params)
            self.daemon._start_view(name, params)
        except (KeyError, ValueError) as err:
            with self._lock:
                self.last_error = "%s: %s" % (type(err).__name__, err)
                self.item_started = self.clock()
                self.dwell = ERROR_DWELL
                self.current_ok = False
            return
        except Exception as err:  # never let a view kill the scheduler
            with self._lock:
                self.last_error = "%s: %s" % (type(err).__name__, err)
                self.item_started = self.clock()
                self.dwell = ERROR_DWELL
                self.current_ok = False
            return
        with self._lock:
            self.last_error = None
            self.item_started = self.clock()
            self.dwell = dwell
            self.current_ok = True
            self.history.append((time.time(), name))
            del self.history[:-HISTORY]

    def _loop(self):
        while not self._stop.is_set():
            cfg = self._config()
            tick = cfg.get("tick_seconds") or 0.2
            try:
                tick = float(tick)
            except (TypeError, ValueError):
                tick = 0.2
            tick = max(0.05, min(2.0, tick))
            if self._hold_reason(cfg) is not None:
                with self._lock:
                    self.item_started = None
                    self.current_ok = False
                self._stop.wait(tick)
                continue
            views = self._views(cfg)
            sig = repr([(v.get("renderer"), v.get("dwell"),
                         repr(v.get("params"))) for v in views])
            with self._lock:
                if sig != self._views_sig:
                    self._views_sig = sig
                    self.index = 0
                    self.item_started = None
                if self.index >= len(views):
                    self.index = 0
                started = self.item_started
                # Effective dwell: a failed show shortens to ERROR_DWELL
                # inside _show_current, so read it back under the lock.
                eff_dwell = self.dwell
                idx = self.index
            item = views[idx % len(views)]
            try:
                dwell = float(item.get("dwell", 30))
            except (TypeError, ValueError):
                dwell = 30.0
            if started is None:
                self._show_current(item, dwell)
            elif self.clock() - started >= eff_dwell:
                with self._lock:
                    self.index = (self.index + 1) % len(views)
                    self.item_started = None
            else:
                try:
                    self.daemon.screen.repaint_overlay()
                except Exception:
                    pass
            self._stop.wait(tick)
