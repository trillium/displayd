# Changelog

All notable changes to displayd, newest first. Every change bumps
`APP_VERSION` in `displayd.py` (the single source of truth) per the
convention in README.md "Versioning": tiny to patch, medium to minor,
large/breaking to major.

## 0.6.0

`deploy.sh` proved its build on every deploy again. The /reload proof step
built its request body with a backslash-continued run of separately-quoted
segments: a line continuation removes the newline but does NOT join two
separately-quoted words into one argv entry (verified against sh, dash,
bash 3.2, bash 5.3 and zsh), so `python3 -c` received only the FIRST segment
as its program, the rest landed in sys.argv, and the segment it did run
printed nothing. RELOAD_BODY came out EMPTY, the proof POSTed an empty body,
the daemon correctly answered 400 "sha is required", and step 4 warned
"reload proof failed; continuing" and skipped -- so the panel stopped
proving which build it was running. Not consecutive-specific: it failed on
every deploy, and the deploy still exited 0 because step 6 gates on other
evidence. An unbuildable body is now a hard deploy failure rather than a
silent skip, so this can never again pass as a skipped proof.

The same idiom hid in two more places in the same script. The transient-view
derivation (the set that decides whether the prior view is a transient) ran
only `import sys,os` and lived on its `|| echo "notice reload"` fallback, so
the repo-derived set its own comment promises was never computed; and
PRE_TRANSIENT was always empty, so the documented "transient active" prior
view fallback could never fire. Both are now single quoted programs; the
derived set still evaluates to exactly `notice reload`, so the fix restores
the documented behaviour rather than changing it.

Every `python3 -c` in deploy.sh must now pass exactly one program argument.
`tests/test_deploy_reload_proof.py` runs the real script against a headless
daemon twice in a row (stubbed ssh/rsync, no live host) and fails if the
proof is skipped or the deploy does not exit 0, plus a lexical guard that
fails on the pre-fix script, so the defect class cannot return silently.

New read-only POST /touch/resolve endpoint: it resolves a tap ({x,y}
or {x_norm,y_norm}) against the current UI and returns the region and
semantic action without dispatching anything -- no state change, no
idle-clock touch. View/mode-gated like the live touch path (same
refusals), so it is intended to be compared against the live touch
path during migration. Version and changelog correction: the merge
that introduced the endpoint left APP_VERSION at 0.4.2; a new public
endpoint is a medium change, so a minor bump.

The touch service exits promptly on SIGTERM now (the P1 restart wedge:
the evdev read loop blocked in a bare read() while the panel sat
untouched, so the stop was never observed and the unit wedged in
`deactivating` -- dead touch input -- until systemd timed out the stop).
The loop waits in a bounded select() poll (0.5s) and the device fd is
nonblocking, so a stop lands within ~a second; `touch-input.service`
also pins `TimeoutStopSec=10` so any future regression fails fast and
loud instead of wedging. `deploy.sh` step 7 verifies rather than
assumes: after the restart it requires ActiveState=active plus a
/touch/check whose announced_at is newer than the pre-restart heartbeat
(the new process announced, not a stale one), and fails loudly
otherwise. `GET /touch/check` agrees after a restart through the normal
path.

## 0.4.0

A tap on a monitor region opens the fullscreen zoom directly (the
captain's bug: taps only warped the cursor, and the zoom needed an
unrequested AIM button). `POST /macbook/mouse` now queues the warp AND
re-pins the showing view to AIM (window start kept) in the same gesture,
so one tap carries the user from the glance map into the fullscreen zoomed
screenshot; a refusal (wrong view, stale feed, tap outside a display)
queues nothing and changes no mode. The AIM button is retired:
`macbook_layout.mode_rect()` and the `mac-to-aim` region are gone, the
glance header's `AIM >` affordance is gone (the Talon mode chip moves
into its slot), and the AIM no-capture hint reads "tap a monitor on
the GLANCE map" instead of "then open AIM". Kept and justified:
`POST /macbook/mode` + the `mac-to-glance` corner (the way back out of
AIM; a harmless no-op in glance) and the `macbook_mode` touch action it
dispatches through -- nothing else wires `aim`. Unchanged: the stage-2
click confirmation inside the zoom (it keys on the queued warp), the
degraded states in both modes, and the coordinate-only tap contract.
Host `touch.json` macbook scope regenerated (6 entries, `mac-to-aim`
removed); `touch.json.example` matches. `GET /touch/check` agrees.

## 0.3.2

App-bar steppers page the strip instead of stepping a highlight. One
press moves the visible window by PAGE_STRIDE (VISIBLE - 1, so the new
window overlaps the old by one chip) with a ~240ms slide (6 frames at
40ms, presented directly past the change-identity dedup), clamped at
both ends -- no wraparound. The press carries the old window in a new
`tab_from` view param so the fresh renderer thread can animate
old-to-new; mode switches drop the hint and draw steady. The `tab`
param is now the first visible chip index (window start); the old
movable highlight is gone entirely (it selected nothing) and the only
emphasis is the Mac's live focused app from the feed (filled chip --
real state). A press at either end is refused (`already at first/last
page`) and that stepper draws dim. `GET /touch/check` geometry is
unchanged (same region ids/rects/actions).

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
