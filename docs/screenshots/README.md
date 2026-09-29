# Screenshots

One genuine frame per advertised view, at the panel's native 1920x1080.
Every PNG here is a real `GET /snapshot` frame from a real daemon run --
never a mockup. `VERSION` records the displayd version that produced the
set (see `GET /version`).

## How they were produced

`python3 tools/capture_screenshots.py` (from the repo root) spawns its own
`DISPLAYD_FAKE_FB=1` daemon on an ephemeral port -- the live panel is never
touched -- enumerates `GET /renderers` at runtime, presents each view with
`POST /show`, waits for a fresh frame, and saves `GET /snapshot` as
`<view>.png` (Pillow `optimize=True`). Policy, feedback, and deploy-stamp
paths point at a temp dir, so a run leaves no state behind. Re-running that
one command reproduces the whole set, including this README's siblings.

The capture needs DejaVu fonts where the daemon looks for them
(`/usr/share/fonts`, see `Screen.font_path`): without them, text-heavy
views fall back to PIL's bitmap font and misrepresent the panel. Linux
hosts already have them; the committed set was taken on macOS via the
container command in the script's docstring, which is the reproducible
path there.

## Seeds and special params

Most views are shown with default params -- exactly what
`POST /show {"renderer": "<view>"}` gives an operator. Exceptions:

- `chat`: two messages pushed first via `POST /feed/chat/message`, so the
  shot shows the populated view rather than the healthy-idle empty state.
- `stream`: one frame pushed via `POST /feed/stream/frame` (the clock
  capture just taken), so the shot shows the view working instead of the
  waiting-for-frame state.
- `image`: shown with `{"path": "<repo>/docs/screenshots/clock.png"}`
  (captured in a second pass) -- the renderer needs a real file.
- `notice` / `qr` / `reload` / `text`: fixed sample params (reload's SHA
  is the checkout's HEAD at capture time, via `DISPLAYD_SHOT_SHA`).

## Honest empty states

Some shots show waiting/error states rather than data, because the
headless capture host has no live sources: `beads` waits for its first
poll, `row` has no journal or PM5 feed (STALE marker path),
`services` reports its inventory URL unreachable, and `unified` shows
the waiting dock (no talon feed -- the tiles above still render). Those
are the views' real no-data screens, documented as such -- not
placeholders.

No view was omitted: the set covers all 24 renderers the daemon
advertised at capture time. If a future `GET /renderers` lists more,
re-run the script and commit the new files.
