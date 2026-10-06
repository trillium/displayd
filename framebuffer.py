"""The physical screen: /dev/fb0 pixels, blanking, and backlight.

Single concept: owning the panel -- presenting frames, the kernel console
handover, screen power (blank + backlight with the restore floor), and the
backlight status the API reports. ``HeadlessFramebuffer`` (the DISPLAYD_FAKE_FB
in-memory double) lives next door in framebuffer_headless.py.
"""

import os
import time

FB = "/dev/fb0"
FB_SYS = "/sys/class/graphics/fb0/"
BACKLIGHT_GLOB = "/sys/class/backlight"
VT = os.environ.get("DISPLAYD_VT", "/dev/tty1")

KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01


def _read(path, default=None):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


class Framebuffer:
    """The physical screen: pixels, blanking, and backlight."""

    def __init__(self):
        self.width = int(_read(FB_SYS + "virtual_size", "1920,1080").split(",")[0])
        self.height = int(_read(FB_SYS + "virtual_size", "1920,1080").split(",")[1])
        self.bpp = int(_read(FB_SYS + "bits_per_pixel", "32"))
        self.stride = int(_read(FB_SYS + "stride", str(self.width * 4)))
        self.fd = os.open(FB, os.O_RDWR)
        self.last_frame = None
        self.blanked = False
        self.saved_brightness = None
        self.backlight = self._find_backlight()
        self.vt_fd = None

    # ---- pixels -------------------------------------------------------

    def present(self, img):
        """Push a PIL RGB image to the screen."""
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))
        data = img.convert("RGB").tobytes("raw", "BGRX")
        self.raw(data)

    def raw(self, data):
        os.pwrite(self.fd, data, 0)
        self.last_frame = data

    def repaint(self):
        if self.last_frame is not None:
            os.pwrite(self.fd, self.last_frame, 0)

    def black(self):
        self.raw(b"\x00" * (self.stride * self.height))

    # ---- console handover ---------------------------------------------

    def take_console(self):
        """Tell the kernel console to stop painting over us."""
        try:
            import fcntl

            self.vt_fd = os.open(VT, os.O_RDWR)
            fcntl.ioctl(self.vt_fd, KDSETMODE, KD_GRAPHICS)
            return True
        except Exception:
            self.vt_fd = None
            return False

    def release_console(self):
        if self.vt_fd is None:
            return
        try:
            import fcntl

            fcntl.ioctl(self.vt_fd, KDSETMODE, KD_TEXT)
        except Exception:
            pass
        finally:
            try:
                os.close(self.vt_fd)
            except OSError:
                pass
            self.vt_fd = None

    # ---- screen power --------------------------------------------------

    def _find_backlight(self):
        try:
            names = sorted(os.listdir(BACKLIGHT_GLOB))
        except OSError:
            return None
        for name in names:
            base = os.path.join(BACKLIGHT_GLOB, name)
            if os.path.exists(os.path.join(base, "brightness")):
                return base
        return None

    def _read_int(self, path):
        val = _read(path)
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    def backlight_state(self):
        if not self.backlight:
            return {"available": False}
        return {
            "available": True,
            "path": self.backlight,
            "value": self._read_int(os.path.join(self.backlight, "brightness")),
            "max": self._read_int(os.path.join(self.backlight, "max_brightness")),
            "saved": self.saved_brightness,
        }

    def set_brightness(self, value):
        """Write brightness, then confirm the driver actually took it."""
        if not self.backlight:
            return False
        maxv = self._read_int(os.path.join(self.backlight, "max_brightness")) or 100
        value = max(0, min(int(value), maxv))
        path = os.path.join(self.backlight, "brightness")
        for _ in range(3):
            try:
                with open(path, "w") as fh:
                    fh.write(str(value))
            except OSError:
                continue
            if self._read_int(path) == value:
                return True
            time.sleep(0.15)
        return False

    # A dimmed or near-zero reading is never a sane restore target: only
    # preserve a value bright enough to actually read (task-i0agw -- the
    # daemon used to capture the dimmed value, leaving the panel near-black
    # after an on/off cycle). Floor is 10% of max, minimum 1.
    def _preserve_floor(self):
        maxv = self._read_int(os.path.join(self.backlight, "max_brightness")) or 100
        return max(1, maxv // 10)

    def _restore_target(self):
        maxv = self._read_int(os.path.join(self.backlight, "max_brightness")) or 100
        floor = self._preserve_floor()
        saved = self.saved_brightness
        if saved and saved >= floor:
            return saved
        return maxv

    def set_blank(self, value):
        try:
            with open(FB_SYS + "blank", "w") as fh:
                fh.write(str(value))
            return True
        except OSError:
            return False

    def get_blank(self):
        return self._read_int(FB_SYS + "blank")

    def power_off(self):
        """Darken the panel for real: backlight first, then the framebuffer."""
        result = {"fb_blank": False, "backlight": False}
        if self.backlight:
            current = self._read_int(os.path.join(self.backlight, "brightness"))
            if current and current >= self._preserve_floor():
                self.saved_brightness = current
            result["backlight"] = self.set_brightness(0)
        result["fb_blank"] = self.set_blank(4)
        self.blanked = True
        return result

    def power_on(self):
        """Bring it back, restoring whatever was on screen."""
        result = {"fb_blank": False, "backlight": False, "repaint": False}
        result["fb_blank"] = self.set_blank(0)
        if self.backlight:
            result["backlight"] = self.set_brightness(self._restore_target())
        self.blanked = False
        self.repaint()
        result["repaint"] = self.last_frame is not None
        return result

    def status(self):
        return {
            "width": self.width,
            "height": self.height,
            "bpp": self.bpp,
            "stride": self.stride,
            "fb_blank": self.get_blank(),
            "backlight": self.backlight_state(),
        }
