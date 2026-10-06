"""The daemon object: construction.

Single concept: building the daemon -- the framebuffer/screen it owns, the
renderer set, the feed store, the policy/playlist/feedback collaborators, and
the shared chrome overlay chain -- plus the frame-cache bookkeeping every
presented frame lands in. The framebuffer backend is constructed through
``self._framebuffer()``, bound by the composition root so the documented
``displayd.Framebuffer`` test hook keeps working.
"""

import os
import threading
import time

import feedback as feedback_module
import playlist as playlist_module
import policy as policy_module
from daemon_power import POLICY_FILE
from feed_store import FeedStore
from renderer_registry import (RENDERER_DIR, load_renderers,
                               system_buttons_module, talon_apps_module)
from screen import Screen


class DisplayCoreMixin:
    """Construction and frame-cache bookkeeping."""

    def __init__(self, policy_path=None, clock=None, feedback_path=None):
        self.fb = self._framebuffer()
        self.screen = Screen(self.fb)
        self.renderers = load_renderers(RENDERER_DIR)
        self.lock = threading.Lock()
        # Long-poll wakeups: every request_* command queue below
        # notifies this condition, so a GET ?wait= hold on a command
        # endpoint wakes the instant a tap queues (never the next
        # poll tick). Own lock -- never held together with self.lock,
        # so queue and wait cannot deadlock.
        self.cmd_cond = threading.Condition()
        self.cache_lock = threading.Lock()
        self.feeds = FeedStore()
        self.screen.feeds = self.feeds
        self.screen.on_present = self._note_frame
        for name, entry in self.renderers.items():
            if "module" not in entry:
                continue
            for input_name, spec in (entry.get("inputs") or {}).items():
                self.feeds.declare(name, input_name, spec)
        # Retired-view feed namespace (see feed() compat): declared cold
        # so /feed/talon_apps/state health reads cold before the first
        # bridge push, exactly like a real input.
        if talon_apps_module is not None:
            try:
                self.feeds.declare("talon_apps", "state",
                                   talon_apps_module.STATE_SCHEMA)
            except Exception:
                pass
        self.frame_cache = {}   # renderer name -> last composed PIL image
        self.switch_pending = None  # start time of the in-flight switch
        self.last_switch_at = None
        self.last_switch_ms = None  # request -> first presented pixel
        self.last_frame_ms = None   # request -> first freshly drawn frame
        # Static-region composition (ISA D6 option B): an opt-in layer
        # over the single-view core. Each region owns a renderer thread
        # drawing into a RegionScreen plus a cached last-good frame;
        # presents recomposite from that per-region cache, so one region
        # updating never disturbs the others. self.lock serializes every
        # mode change; layout_lock guards the region tables only.
        self.layout_lock = threading.Lock()
        self.layout = None  # list of bound regions or None (single mode)
        self.region_threads = {}  # region name -> thread record
        self.region_frames = {}   # region name -> last-good PIL frame
        self.region_errors = {}   # region name -> "Error: detail"
        self.region_updated = {}  # region name -> timestamp of last frame
        self.layout_started_at = None
        self.layout_pending = None  # start time of the in-flight layout
        self.layout_switch_ms = None  # request -> first composited pixel
        self.layout_gen = 0  # bumps on every mode change; stale presents bail
        # Policy layer: activity clock, transient switching, idle-off.
        # Only mutating POSTs touch the clock -- GETs (including the
        # control page's 2s state/snapshot poll) are observation, not
        # activity, or idle-off could never fire while the page is open.
        self.policy = policy_module.Policy(
            policy_path if policy_path is not None else POLICY_FILE,
            clock=clock)
        # Display-feedback log (feedback.py): durable JSONL + per-note PNG
        # frames. feedback_path is a test hook; production uses the env
        # default next to the daemon, mirroring POLICY_FILE.
        self.feedback = feedback_module.FeedbackStore(path=feedback_path)
        self.transient_timer = None
        # Reload scan-relay: one-time tokens -> {sha, commit_url,
        # expires_at}. Guarded by self.lock; pruned on every issue/scan.
        # No tracking beyond the confirm event itself: entries hold only
        # what the redirect needs, and single-scan consumption deletes
        # them outright.
        self.reload_tokens = {}
        self.reload_token = None  # token of the currently showing reload
        # MacBook cursor slot (POST /macbook/mouse, fetched by the
        # Mac-side poller): latest pending {x, y, display_index, ts, id}
        # in Quartz coordinates, or None. Single slot, TTL-expiry -- a
        # tap never fires minutes late. Guarded by self.lock.
        # Playlist rotation: scheduler on top of _start_view, overlay hook
        # for the progress bar. Starts enabled only from persisted config.
        self.playlist = playlist_module.Playlist(self)
        # Shared screen chrome, composed not replaced: the playlist bar
        # and the system buttons draw through one chained overlay so all
        # stay visible at once (a second plain assignment here would
        # silently disable the earlier layers). The component layer owns
        # the chain, so a failing layer is skipped instead of blanking
        # the panel; each badge reads screen.current_view live and
        # suppresses itself where it is meaningless. See
        # renderers/ui/system_buttons.py.
        self.screen.overlay = self.playlist.overlay_image
        if system_buttons_module is not None:
            self.screen.overlay = system_buttons_module.system_overlay(
                self.screen, self.screen.overlay)
        self.watchdog_stop = threading.Event()
        self.watchdog_thread = None
        self.stop_event = None
        self.thread = None
        self.current = None
        self.current_params = None  # params the showing view was started with
        self.started_at = None
        self.last_error = None
        # Last POST /touch/announce: the region set the running touch
        # service is actually dispatching (None until it announces).
        self.touch_live = None
        # Pre-sleep return target ({"renderer", "params"}), captured
        # by the power-off path and consumed by the power-on path. None
        # means no return pending (never slept, or already restored).
        self.sleep_restore = None
        self.playlist.start()
        self.console_taken = self.fb.take_console()
        self.fb.set_blank(0)
        if self.fb.backlight:
            maxv = self.fb._read_int(os.path.join(self.fb.backlight, "max_brightness")) or 100
            cur = self.fb._read_int(os.path.join(self.fb.backlight, "brightness")) or 0
            if cur == 0:
                self.fb.set_brightness(maxv)

    def _note_frame(self, img):
        """Every composed frame lands here: cached per view for instant
        re-entry, and timed when a switch is in flight. Never blocks: the
        copy is a memcpy, no I/O."""
        now = time.time()
        with self.cache_lock:
            if self.current is not None:
                try:
                    self.frame_cache[self.current] = img.copy()
                except Exception:
                    pass
            if self.switch_pending is not None:
                self.last_frame_ms = round((now - self.switch_pending) * 1000, 1)
                if self.last_switch_ms is None:
                    # No cached frame covered this switch: the fresh draw
                    # is also the first pixel.
                    self.last_switch_ms = self.last_frame_ms
                self.switch_pending = None
