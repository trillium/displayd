"""The in-memory framebuffer double (DISPLAYD_FAKE_FB=1).

Single concept: standing in for /dev/fb0 where there is no panel -- headless
CI and local MCP demos. Drawing goes to an in-memory BGRX buffer with the same
layout snapshot() expects, so show/feed/snapshot behave identically minus
photons. The deployed daemon never selects it.
"""

from framebuffer import Framebuffer

class HeadlessFramebuffer(Framebuffer):
    """In-memory stand-in for the physical screen.

    Active ONLY when DISPLAYD_FAKE_FB=1 (headless CI and local MCP demos
    with no /dev/fb0). The deployed daemon never sets it. Drawing goes to
    an in-memory BGRX buffer with the same layout snapshot() expects, so
    show/feed/snapshot behave identically minus photons."""

    def __init__(self, width=1920, height=1080):
        self.width = width
        self.height = height
        self.bpp = 32
        self.stride = self.width * 4
        self.fd = None
        self.last_frame = None
        self.blanked = False
        self.saved_brightness = None
        self.backlight = None
        self.vt_fd = None

    def raw(self, data):
        self.last_frame = bytes(data)

    def repaint(self):
        pass

    def black(self):
        self.raw(bytes(self.width * self.height * 4))

    def take_console(self):
        return False

    def release_console(self):
        pass

    def set_blank(self, value):
        self.blanked = (int(value) != 0)
        return True

    def get_blank(self):
        return 4 if self.blanked else 0

    def power_off(self):
        self.set_blank(4)
        return {"fb_blank": True, "backlight": False}

    def power_on(self):
        self.set_blank(0)
        return {"fb_blank": True, "backlight": False,
                "repaint": self.last_frame is not None}
