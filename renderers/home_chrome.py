"""Persistent home affordance for the jumbotron. NOT a renderer: no
run(), so the daemon's loader skips this file (same convention as
qr_common.py / beads_common.py).

One small module owns all shared screen chrome so individual renderers
never draw it: the home button is composited onto every presented frame
through the ``Screen.overlay`` hook in displayd.py, next to the playlist
progress bar. Per-renderer drawing would redesign every screen type;
this draws once for all of them.

Pieces:

- ``chain_overlays(*fns)`` -- the composition primitive. Applies each
  overlay in order (playlist bar first, home button last so the
  affordance stays on top), tolerates None entries, and never raises:
  a failing layer is skipped so one broken chrome can never blank the
  panel. The frame cache keeps pre-overlay frames (see Screen.present),
  so a cached re-entry never serves a stale badge -- the same discipline
  as the progress bar.
- ``draw_home_button(img)`` -- pure draw of the top-left badge: a dark
  rounded tile with a white house glyph, no font needed. Reads on dark
  views (beads) and light ones (qr) alike.
- ``home_overlay(screen)`` -- overlay-fn factory. Suppressed on views
  where the button is meaningless or harmful (see SUPPRESSED_VIEWS), so
  the reload QR stays fully scannable and its generous tap-to-dismiss
  keeps every corner pixel.
- ``home_rect(w, h)`` / ``home_region(...)`` -- touch.json geometry for
  the button: top-left, inside the left gesture strip's width so it never
  covers picker tiles (the grid starts at x=160). List it FIRST in
  "regions": hit_test() gives earlier entries every overlap, so the
  button wins the strip's top square while the rest of the strip still
  fires screen_on. The region reuses the existing ``select_view`` action
  (fixed-shape POST /show, no new action).

Suppression (deliberate, see TOUCH.md "Home button"):

- ``picker`` -- the button is meaningless on the selection screen
  itself; a tap there re-shows the picker (harmless no-op, same contract
  as the options view), so nobody is trapped.
- ``reload`` -- the deploy-proof confirmation keeps its full-screen
  dismiss area: every tap still POSTs /touch/tap first, and the corner
  tap then also navigates per normal region rules.
- ``notice`` -- short-lived transient; matches the playlist bar, which
  also hides while a transient holds the screen.
"""

HOME_STRIP = 160  # button lives inside the left gesture strip's width,
# so it never covers picker tiles (the grid starts at x=160).
HOME_VIEW = "picker"  # select_view target: the view-selection screen.
SUPPRESSED_VIEWS = ("picker", "reload", "notice")

BADGE_FILL = (13, 17, 28)
BADGE_EDGE = (255, 255, 255)
GLYPH = (255, 255, 255)


def home_rect(w=1920, h=1080):
    """Button square [x, y, rw, rh] at the top-left, clamped to small
    screens. Pure."""
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return [0, 0, HOME_STRIP, HOME_STRIP]
    if w <= 0 or h <= 0:
        return [0, 0, HOME_STRIP, HOME_STRIP]
    side = max(64, min(HOME_STRIP, w // 4, h // 4))
    return [0, 0, side, side]


def home_region(w=1920, h=1080, view=HOME_VIEW):
    """touch.json region entry for the button. Paste FIRST under
    "regions" (earlier entries win overlaps), ahead of the picker tiles
    and the full-height gesture strips. Pure."""
    return {"id": "home",
            "rect": home_rect(w, h),
            "action": {"name": "select_view", "view": view}}


def chain_overlays(*fns):
    """Compose overlay fns into one ``fn(img) -> img`` for Screen.overlay.

    Applies each non-None layer in order and returns the composed frame.
    Never raises: a layer that errors (or returns None) is skipped, so
    one broken chrome can never blank the panel. Pure factory."""
    layers = [fn for fn in fns if fn is not None]

    def apply(img):
        for fn in layers:
            try:
                out = fn(img)
            except Exception:
                continue
            if out is not None:
                img = out
        return img

    return apply


def draw_home_button(img, rect=None):
    """Paste the home badge onto an already-composed full-screen frame.

    `img` is the PIL image about to be presented; `rect` defaults to
    home_rect() for its size. Returns img. Never raises: on any drawing
    failure the frame goes out untouched (a missing badge beats a
    missing frame)."""
    try:
        from PIL import ImageDraw

        w, h = img.size
        rect = list(rect) if rect else home_rect(w, h)
        x0, y0, rw, rh = (int(v) for v in rect)
        side = max(16, min(rw, rh))
        pad = max(2, side // 18)
        bx0, by0, bx1, by1 = x0 + pad, y0 + pad, x0 + side - pad, y0 + side - pad
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([bx0, by0, bx1, by1],
                               radius=max(4, side // 8),
                               fill=BADGE_FILL, outline=BADGE_EDGE,
                               width=max(2, side // 48))
        # House glyph: roof polygon over a body rect, door cut out in
        # the badge fill. All proportional so small screens stay legible.
        bw = bx1 - bx0
        cx = (bx0 + bx1) // 2
        roof_y = by0 + int(bw * 0.30)
        eave_y = by0 + int(bw * 0.52)
        body_y1 = by1 - int(bw * 0.10)
        inset = int(bw * 0.22)
        eave = int(bw * 0.10)
        draw.polygon([(cx, roof_y),
                      (bx1 - eave, eave_y),
                      (bx0 + eave, eave_y)], fill=GLYPH)
        draw.rectangle([bx0 + inset, eave_y, bx1 - inset, body_y1],
                       fill=GLYPH)
        dw = max(3, int(bw * 0.12))
        dh = max(4, int(bw * 0.20))
        draw.rectangle([cx - dw, body_y1 - dh, cx + dw, body_y1],
                       fill=BADGE_FILL)
    except Exception:
        pass
    return img


def home_overlay(screen, suppressed=SUPPRESSED_VIEWS, rect=None):
    """Overlay-fn factory for Screen.overlay: draw the button on every
    view except the suppressed ones. Reads ``screen.current_view`` live,
    so transients (reload/notice) and the picker itself stay chrome-free
    without any per-renderer code. Pure factory; never raises."""

    def apply(img):
        try:
            if getattr(screen, "current_view", None) in (suppressed or ()):
                return img
            return draw_home_button(img, rect)
        except Exception:
            return img

    return apply


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Print the touch.json home-button region entry "
                    "(default 1920x1080). Paste FIRST under \"regions\" "
                    "(earlier entries win overlaps), ahead of the picker "
                    "tiles and gesture strips. Fires the existing "
                    "select_view action -- no new action.")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--view", default=HOME_VIEW)
    args = ap.parse_args()
    print(json.dumps(home_region(args.width, args.height, args.view),
                     indent=2))
