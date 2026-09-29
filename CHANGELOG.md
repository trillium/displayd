# Changelog

All notable changes to displayd, newest first. Every change bumps
`APP_VERSION` in `displayd.py` (the single source of truth) per the
convention in README.md "Versioning": tiny to patch, medium to minor,
large/breaking to major.

## 0.3.1

Touch regions follow the Talon redesign (the 0.3.0 deploy refused to
complete: drawn UI and live regions disagreed). Host `touch.json`
regenerated from the renderers' own region functions, never
hand-computed -- `macbook_layout.touch_regions()` for the 7-entry
macbook scope (the AIM button, back corner, both tab steppers, focus
chip strip, map, and AIM click catcher), `picker_regions()` for the
19-tile picker scope (`talon_apps` out, `unified` in), and
`unified_regions()` entries `[1:]` for the 19-entry unified scope
(18 tiles at real geometry plus the apps dock). The retired
`talon_apps` scope is gone from the live set; the global home badge
now targets `unified` (`HOME_VIEW`). `renderers/macbook_zoom.py`
deleted: dead since 0.3.0 superseded it with `macbook_aim.py` +
`macbook_glance.py` (nothing imported it). `touch-picker.json.example`
regenerated (was 17 stale tiles); `touch-unified.json.example` already
matched. TOUCH.md's unified generation command fixed to exclude the
self tile (passing the full set silently shifts every tile rect).
`GET /touch/check` is the drift guard: it failed the 0.3.0 deploy
loudly and now reports agreement.

## 0.3.0

Merged Talon feature (GLANCE + AIM modes, app list folded in).
`renderers/macbook.py` is ONE feature with two modes, each claiming the
full canvas -- the three fixed bands (210px header, map, bottom image)
and the separate `talon_apps` view are gone:

- GLANCE (default): slim full-width header (148px, not 210) --
  tab-through app strip, focused app + window, mouse position, Talon
  mode -- with the display map filling everything below it. No
  screenshot.
- AIM: the fresh review capture fills the screen edge-to-edge
  (cover-fit, crosshair, scale + age caption) and the pointer position
  is deliberately dropped. Tap the image to click the reviewed point.
- "Tab through", mechanically: the header shows a scrolling window of
  the live apps; `POST /talon/tab` steps the highlight with wraparound
  (daemon keeps the mode); tapping a chip focuses that app
  (coordinate-only, as before). `POST /macbook/mode` pins GLANCE/AIM
  (daemon keeps the tab). Both are closed touch actions
  (`macbook_mode`, `talon_tab`) with static bodies.
- `renderers/macbook_layout.py` (new, pure): the single source of truth
  the renderer draws from, the daemon hit-tests against, and the touch
  regions generate from -- draw, tap, and region cannot drift (same
  contract `talon_layout.py` held for the old view). Drawing split per
  the 250-line budget: `macbook_glance.py` + `macbook_aim.py`.
- The standalone `talon_apps` view is RETIRED (not aliased): one feature,
  one name in `GET /renderers`, picker, and screenshots. Its feed
  namespace survives -- the Mac-side poller still posts
  `POST /feed/talon_apps/state` unchanged (daemon feed compat, validated
  against the helper schema, stored under the same key), and the
  unified dock still reads that feed in place (its tap now opens
  `macbook`). `talon_apps.py` keeps the pure helpers
  (`clean`/`label`/`groups`); `talon_layout.py` still serves the dock.
- Click delivery untouched (warp-then-click, file channel, 150ms tick)
  and every tap stays coordinate-only. All slots are mode-gated now:
  map/focus/tab fire in GLANCE only, click in AIM only -- misses are
  409, never mis-fires. Degraded states survive in BOTH modes (waiting
  / STALE / app-only; AIM with no capture shows a hint, never blank).
- Touch: `macbook` scope regenerated (mode corners, steppers, chip
  strip, map, fullscreen click catcher last); `talon_apps` scope
  removed; unified scope drops the `talon_apps` tile (18 tiles).
- `docs/screenshots/macbook.png`: genuine waiting capture of the merged
  view; `talon_apps.png` removed; `unified.png`/`picker.png` refreshed
  (tile set changed).

## 0.2.0

Merged home screen (E layout: picker tiles + live apps dock).
`HOME_VIEW` moves from `picker` to the new `unified` renderer -- the
home badge and home region now target the merged screen; the bare
`picker` stays as a selectable view.

- `renderers/unified.py` (new): 19 tiles at real picker geometry via
  `picker.draw`/`grid_geometry` plus a full-width dock summarising the
  talon_apps feed in place (count + focused app + LEFT/RIGHT split +
  mode line, one tap to the full grouped screen). Empty (no payload)
  and stale (quiet past `STALE_AFTER`) render inside the dock only --
  tiles never move. Tile list defaults to the live advertised set
  minus `unified` itself; explicit `views` wins.
- `renderers/unified_dock.py` (new): dock content + drawing (kept
  separate per the 250-line budget, same pattern as `row_draw.py`).
- `renderers/home_chrome.py`: `HOME_VIEW = "unified"`, `unified` added
  to `SUPPRESSED_VIEWS` (alongside `picker`).
- `renderers/talon_layout.py`: `group()` also returns uncapped
  `left_total`/`right_total` for the dock's split line (additive).
- Touch: `unified_regions()` generates the 21-entry map (sleep first,
  19 `uview-` tiles, `apps-dock` last -- the prefix keeps picker and
  unified scopes id-unique for the announce gate); `touch_audit` and
  `GET /touch/check` assert it like the picker grid. New
  `touch-unified.json.example`; host post-deploy steps (refresh the
  global home region, add the `unified` scope) in TOUCH.md.
- `docs/screenshots/unified.png`: genuine waiting-dock capture (no
  talon feed on the capture host) via `tools/capture_screenshots.py`.

## 0.1.0

First versioned release. No behaviour change — this release only introduces
the version itself:

- `APP_VERSION = "0.1.0"` in `displayd.py`, reported via `GET /version`,
  the `"version"` key of `GET /state`, and the startup log line.
- This changelog and the semver convention documented in README.md.
- `docs/screenshots/`: a genuine `DISPLAYD_FAKE_FB=1` snapshot of every
  advertised view, taken at this version, with a one-command regeneration
  script (`tools/capture_screenshots.py`).
