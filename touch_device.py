"""The input device for the touch service: enumeration and the read loop.

Single concept: the input device itself -- enumerating ``/dev/input/event*``
with their sysfs names, reading one, and feeding decoded events to the tap
path. The wait is a bounded select() poll (READ_POLL_SECONDS) so a
SIGINT/SIGTERM while the panel sits untouched still exits; the fd is
nonblocking so a spurious ready never stalls past the stop flag. ``run`` is
the foreground entry; opening the device and the dry-run decision stay here.
"""

import glob
import logging
import os
import select

from touch_events import EVENT_SIZE, READ_POLL_SECONDS, parse_event

LOG = logging.getLogger("displayd-touch")


def list_input_devices():
    """Enumerate /dev/input/event* with sysfs names. Best-effort: missing
    sysfs entries yield name=None rather than failing."""
    found = []
    for path in sorted(glob.glob("/dev/input/event*")):
        name = None
        try:
            num = "".join(c for c in os.path.basename(path)
                          if c.isdigit())
            candidates = glob.glob(
                "/sys/class/input/event%s/device/name" % num)
            if candidates:
                with open(candidates[0], "r", encoding="utf-8",
                          errors="replace") as fh:
                    name = fh.read().strip()
        except OSError:
            pass
        found.append({"path": path, "name": name})
    return found


class DeviceLoopMixin:
    """Device read loop and foreground run for the touch service."""

    def iter_device_events(self, stream):
        """Yield (type, code, value) triples from a raw device stream,
        tolerating short reads at EOF.

        Each read() requests exactly the remainder of one 24-byte
        input_event record. The kernel validates the count against its
        native record size, so requesting anything other than a full
        record (e.g. the old 16-byte size) fails with EINVAL -- hence
        the pinned EVENT_SIZE above.

        The wait for the next bytes is a bounded select() poll, not a
        bare blocking read: on timeout the loop re-checks the stop flag,
        so a SIGTERM/SIGINT while the panel sits untouched exits within
        ~READ_POLL_SECONDS instead of wedging the unit in `deactivating`
        until systemd times out the stop. Streams without a selectable
        fd (BytesIO, test fakes) fall through to the plain blocking
        read, exactly as before."""
        buf = b""
        while not self._stop:
            if not self._wait_readable(stream):
                continue  # poll timeout: re-check the stop flag
            try:
                chunk = stream.read(EVENT_SIZE - len(buf))
            except BlockingIOError:
                continue  # nonblocking fd, nothing yet: re-poll
            if chunk is None:
                continue  # nonblocking FileIO reports no-data-yet as
                # None (not b""): re-poll, do NOT mistake it for EOF
            if not chunk:
                if buf:
                    raise ValueError("truncated input_event at EOF")
                return
            buf += chunk
            if len(buf) < EVENT_SIZE:
                continue
            yield parse_event(buf)
            buf = b""

    @staticmethod
    def _wait_readable(stream):
        """Block (bounded) until stream is readable; False on timeout.

        True (readable, or unselectable) means the caller proceeds to
        read(); False means the poll timed out and the caller re-checks
        the stop flag. Any stream that cannot take part in select()
        returns True immediately -- the blocking read path, unchanged."""
        try:
            fd = stream.fileno()
        except Exception:
            return True
        try:
            ready, _, _ = select.select([fd], [], [], READ_POLL_SECONDS)
        except Exception:
            return True  # unselectable fd: blocking read, as before
        return bool(ready)

    def run(self, dry_run=False):
        device = self.config["device"]
        LOG.info("touch service starting: device=%s endpoint=%s "
                 "display=%dx%d%s", device, self.config["endpoint"],
                 self.config["width"], self.config["height"],
                 " (dry-run, no HTTP)" if dry_run else "")
        try:
            stream = open(device, "rb", buffering=0)
        except FileNotFoundError:
            LOG.error("device %s not found; run --list-devices", device)
            raise SystemExit(2)
        except PermissionError:
            LOG.error("permission denied on %s; see TOUCH.md "
                      "(input group / udev rule)", device)
            raise SystemExit(2)
        with stream:
            # Nonblocking device fd: a spurious select()-ready followed
            # by a blocking read() could stall past the stop flag, so
            # the fd itself must not block either (short reads still
            # reassemble in iter_device_events; BlockingIOError re-polls).
            try:
                os.set_blocking(stream.fileno(), False)
            except Exception:
                pass  # best-effort: the select poll still bounds the wait
            if not dry_run:
                # Heartbeat renewal, in-process: the startup announce
                # in main() is not enough -- a long-lived service (or a
                # daemon restart wiping the daemon-side heartbeat) would
                # otherwise leave /touch/check blind until the next
                # deploy-time restart. No restart dependency (see the
                # P1 restart wedge): one cheap POST per interval.
                self.start_heartbeat()
            try:
                for ev_type, code, value in self.iter_device_events(stream):
                    for event in self.parser.feed(ev_type, code, value):
                        LOG.debug("touch %s", event)
                        self.handle_frame([event], dry_run=dry_run)
                    if self._stop:
                        break
            finally:
                self.stop_heartbeat()
        LOG.info("touch service stopped")
