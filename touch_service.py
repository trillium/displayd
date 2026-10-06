"""The touch service: the class that composes the tap-path concepts.

Single concept: the service object itself -- construction (config, client,
heartbeat callable, tap detector), the stop flag, and the mixin set that
gives it its behaviour. The concepts live next door: heartbeat renewal
(touch_heartbeat), live scope (touch_scope), confidence feedback
(touch_feedback), tap dispatch (touch_dispatch) and the device read loop
(touch_device). The class name and attribute surface are unchanged, so
``touch.TouchService`` keeps meaning exactly what it meant before.
"""

import logging
import threading

from touch_client import DisplaydClient, post_announce
from touch_config import normalize_confidence_feedback, normalize_tap_options
from touch_device import DeviceLoopMixin
from touch_dispatch import TapDispatchMixin
from touch_events import EvdevParser
from touch_feedback import FeedbackMixin
from touch_heartbeat import ANNOUNCE_INTERVAL_SECONDS, HeartbeatMixin
from touch_scope import ScopeMixin
from touch_taps import TapDetector

LOG = logging.getLogger("displayd-touch")


class TouchService(HeartbeatMixin, ScopeMixin, FeedbackMixin,
                   TapDispatchMixin, DeviceLoopMixin):
    """Foreground reader: device -> parser -> normalize -> tap -> action."""

    def __init__(self, config, client=None, clock=None, announce=None):
        self.config = config
        self.client = client or DisplaydClient(config["endpoint"])
        self.announce = announce or (lambda: post_announce(self.config))
        self.announce_interval = float(
            config.get("announce_interval") or ANNOUNCE_INTERVAL_SECONDS)
        self.confidence = normalize_confidence_feedback(
            config.get("confidence_feedback"))
        self.tap_options = normalize_tap_options(
            config.get("tap_options"))
        self.parser = EvdevParser()
        self.taps = TapDetector(
            tap_max_seconds=config.get("tap_max_seconds", 0.5),
            tap_max_pixels=config.get("tap_max_pixels", 40),
            debounce_seconds=config.get("debounce_seconds", 0.3),
            clock=clock)
        self._stop = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None

    def request_stop(self, *_args):
        LOG.info("touch service stopping")
        self._stop = True
