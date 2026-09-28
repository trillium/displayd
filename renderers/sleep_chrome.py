"""Persistent sleep affordance for the jumbotron. NOT a renderer: no
run(), so the daemon's loader skips this file (same convention as
home_chrome.py / qr_common.py).

The findable half of panel sleep: a top-right moon badge composited onto
every presented frame through the ``Screen.overlay`` hook in displayd.py,
next to the playlist bar and the home button. Its touch.json region
(screen_off, the existing closed action driving POST /screen/off) sits
FIRST among the right-side entries so it wins the strip's top square
while the rest of the strip still advances the playlist -- the same
overlap discipline as the home button on the left strip.

The wake half lives on the other side: powering off also switches to
the dedicated sleep view (renderers/sleep.py), whose per-view region
set holds exactly one fullscreen screen_on target. Scoped regions are
hit-tested before global ones, so while asleep that wake target shadows
every global strip (nothing else is reachable, nothing is shadowed while
awake -- the wake region is simply not live on any other view).

Pieces (mirroring home_chrome.py):

- ``sleep_rect(w, h)`` / ``sleep_region(...)`` -- touch.json geometry:
  top-right, inside the right gesture strip's width so it never covers
  view content (picker grid ends at x=1760, the macbook map starts at
  y=250, talon columns start at y=250). List it before "playlist-next":
  hit_test() gives earlier entries every overlap.
- ``draw_sleep_button(img)`` -- pure draw of the badge: the same dark
  rounded tile as home, moon glyph (disc with an offset cutout), no font
  needed. Reads on dark views and light ones alike.
- ``sleep_overlay(screen)`` -- overlay-fn factory. Suppressed on sleep
  (nothing to sleep while asleep), reload (the QR stays fully scannable),
  and notice (short-lived transient, matching the playlist bar). VISIBLE
  on picker: unlike home (which would just re-show the picker), sleeping
  from the selection screen is meaningful and the corner is tile-free.
"""

SLEEP_STRIP = 160  # badge lives inside the right gesture strip's width,
# so it never covers view content (same discipline as HOME_STRIP).
SUPPRESSED_VIEWS = ("sleep", "reload", "notice")

BADGE_FILL = (13, 17, 28)
BADGE_EDGE = (255, 255, 255)
GLYPH = (255, 255, 255)


def sleep_rect(w=1920, h=1080):
    """Button square [x, y, rw, rh] at the top-right, clamped to small
    screens. Pure."""
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return [1920 - SLEEP_STRIP, 0, SLEEP_STRIP, SLEEP_STRIP]
    if w <= 0 or h <= 0:
        return [1920 - SLEEP_STRIP, 0, SLEEP_STRIP, SLEEP_STRIP]
    side = max(64, min(SLEEP_STRIP, w // 4, h // 4))
    return [w - side, 0, side, side]


def sleep_region(w=1920, h=1080):
    """touch.json region entry for the button. Paste before
    "playlist-next" (earlier entries win overlaps). Fires the existing
    screen_off action -- no new action. Pure."""
    return {"id": "screen-off",
            "rect": sleep_rect(w, h),
            "action": {"name": "screen_off"}}


def draw_sleep_button(img, rect=None):
    """Paste the moon badge onto an already-composed full-screen frame.

    `img` is the PIL image about to be presented; `rect` defaults to
    sleep_rect() for its size. Returns img. Never raises: on any drawing
    failure the frame goes out untouched (a missing badge beats a
    missing frame)."""
    try:
        from PIL import ImageDraw

        w, h = img.size
        rect = list(rect) if rect else sleep_rect(w, h)
        x0, y0, rw, rh = (int(v) for v in rect)
        side = max(16, min(rw, rh))
        pad = max(2, side // 18)
        bx0, by0, bx1, by1 = x0 + pad, y0 + pad, x0 + side - pad, y0 + side - pad
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([bx0, by0, bx1, by1],
                               radius=max(4, side // 8),
                               fill=BADGE_FILL, outline=BADGE_EDGE,
                               width=max(2, side // 48))
        # Moon glyph: full disc with an offset badge-coloured cutout,
        # all proportional so small screens stay legible.
        bw = bx1 - bx0
        cx = (bx0 + bx1) // 2
        cy = (by0 + by1) // 2
        r = bw * 0.30
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GLYPH)
        ox, oy, orad = cx + r * 0.55, cy - r * 0.35, r * 0.85
        draw.ellipse([ox - orad, oy - orad, ox + orad, oy + orad],
                     fill=BADGE_FILL)
    except Exception:
        pass
    return img


def audit_exact(view, w=1920, h=1080):
    """touch_audit.py projection: [exact entry] drawn here, or [] when
    this view suppresses the badge. Pure."""
    if view in (SUPPRESSED_VIEWS or ()):
        return []
    badge = sleep_region(w, h)
    return [{"id": badge["id"],
             "rect": [int(v) for v in badge["rect"]],
             "action": None, "required": True}]


def sleep_overlay(screen, suppressed=SUPPRESSED_VIEWS, rect=None):
    """Overlay-fn factory for Screen.overlay: draw the button on every
    view except the suppressed ones. Reads ``screen.current_view`` live,
    so sleep/reload/notice stay chrome-free without per-renderer code.
    Pure factory; never raises."""

    def apply(img):
        try:
            if getattr(screen, "current_view", None) in (suppressed or ()):
                return img
            return draw_sleep_button(img, rect)
        except Exception:
            return img

    return apply


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Print the touch.json sleep-button region entry "
                    "(default 1920x1080). Paste before \"playlist-next\" "
                    "(earlier entries win overlaps). Fires the existing "
                    "screen_off action -- no new action.")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    args = ap.parse_args()
    print(json.dumps(sleep_region(args.width, args.height), indent=2))
