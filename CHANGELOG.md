# Changelog

All notable changes to displayd, newest first. Every change bumps
`APP_VERSION` in `displayd.py` (the single source of truth) per the
convention in README.md "Versioning": tiny to patch, medium to minor,
large/breaking to major.

## 0.8.1

**The reload-proof warning on consecutive deploys is already fixed on `main`;
this release repairs the test that pins it** (task-34pp2). The warn-and-skip
was the empty `/reload` body fixed in 0.6.0 (#38). Reproduced against the
fixture: two back-to-back `deploy.sh` runs on current `main` both show
"reload confirmation showing on panel" and "reload confirmed" once the fixture
reaches the proof. It did not reach it in a worktree with no built litehtml
engine: the html-runtime gate (a hard failure since #42) aborted the deploy
at step 2b, and the fixture's old comment still expected a warning there. The
fixture now answers that gate "complete" (the gate keeps its own tests in
`test_deploy_html_step.py`), so the consecutive-deploy regression test runs
anywhere. No `deploy.sh` or daemon change; a genuinely failing proof still
warns, and a real runtime gap still fails the deploy. Automated fixture proof
only; no live deploy was run.

## 0.8.0

The chat panel is the two-pane panel the captain asked for: the viewers in the
channel on the left, the chat on the right, and user-join events on the chat
stream itself (task-vd6qk, task-3yep9).

**Presence needed a source, and the documented one was wrong.** The scout
report and the beads both point at `GET /api/v1/viewers`, so it was read off the
Firebot 5.66.7 tree rather than trusted: that endpoint is the viewer DATABASE
(`getAllUsernamesWithIds` projects `{_id, username, displayName}` over
`{twitch: true}`), every viewer Firebot has ever recorded, with no presence
field at all. Drawing it under "who is here now" would have fabricated
presence. `GET /api/v1/viewers/export` is the same documents unprojected, so
each one carries Firebot's own `online` flag -- the one `ActiveUserHandler`
flips on a 450 s TTL, fed by the Helix chatter poll every 5 minutes and by every
chat message. That is Firebot's own answer to "who is presently in the channel",
and its TTL is the documented lag: an arrival shows as soon as Firebot sees the
user, a quiet departure within ~7.5 minutes. It is the whole viewer database, so
it is the wrong poll on a large channel; `--roster-url` points the poll
elsewhere.

`bridges/firebot_roster.py` owns that read and its two pure halves (the export
payload -> a roster, and a roster diff -> arrivals). The bridge runs one poll per
cycle in a thread beside the socket (default 10 s) and pushes BOTH answers off
that SINGLE read: the snapshot to `/feed/chat/roster`, and each arrival to
`/feed/chat/message` as a join event, so the panel interleaves joins with
messages by arrival and never holds two answers to the same question. Joins are
announced, departures are not -- the roster pane drops them, and the request
named joins. A failed read keeps the last-known roster (a blip must not
re-announce the channel), the first read only establishes a baseline (a restart
must not either), and the bridge logs the failure without taking the socket down.

`renderers/chat.py` is a template surface now, drawn by litehtml from
`html-templates/chat.html` with the geometry in `renderers/chat_panes.py` and
the text fitting in `renderers/chat_fit.py`, so the panel reads across a room and
its rows are measured rather than hoped for. Every pushed display name and
message is escaped into the one `{{panes|raw}}` slot, and the only colours there
come from palette tuples the screen already parsed. Roster rows are
alphabetical with a `+N more` overflow row, the pane head's count is the same
number the pane draws, and a roster whose pushes stopped says `ROSTER STALE`
with the age instead of showing last-known presence as live.

Retention is untouched and re-pinned: a message leaves state only on a
moderation delete, never for age or count, and join events are retained the same
way. Only the visible window is screen-bounded, budgeted from the newest event
backwards so the line that just arrived is always on screen -- which also fixes
the old loop's quiet failure of drawing past the bottom and cutting the newest
message off. With nothing pushed at all the panel still says `waiting for chat`
and draws no panes around it (project-a4t.8.1).

The chat view therefore needs the built engine, exactly as `html`, `picker`,
`options` and the home screen do; without it the panel shows the red build card.
`bridges/firebot_roster.py` joins `bridges/mac-set.manifest`, so
`bridges/install-mac.sh` deploys it with the bridge it belongs to -- until the
MacBook's bridge is reinstalled the panel honestly shows no roster rather than a
stale one.

Minor bump: a new user-visible feature (the two-pane panel and join
events) landing after 0.7.0. No public endpoint shape changed, no other
view's behaviour changed, and the chat view's own contract only gained an
input (`roster`) and a flag (`join` on `message`).

## 0.7.0

The panel's UI shell renders through litehtml. The chrome every view sits
inside is one template now, `html-templates/layout.html` -- header, the two
side gesture strips, the content band and the footer -- with a fixed,
documented variable contract that the html renderer fills; an empty
variable set on that template draws `DEFAULT_VARS`, so the default view is
a usable panel rather than a red card the instant it is tapped. The html
view needs no required param any more, and that is what puts it in the live
picker/home set and in rotation beside every other view.

Three surfaces that used to draw in Pillow moved onto that engine, each
keeping its rect exactly where its touch regions already were: the picker
(`html-templates/picker.html` + `renderers/_picker_tiles.py`), the merged
home screen's apps dock (`html-templates/dock.html` +
`renderers/unified_dock.py`), and the options screen
(`html-templates/options.html` + `renderers/_options_grid.py`). The picker
compensates for the one way litehtml and the touch layer disagree about a
tile: a declared width is the CONTENT box, so an uncompensated tile would
be 12px wider than the region that taps it. The dock keeps its authored
`DESIGN` size and is drawn by pasting only the dock rect, so
`renderers/unified.py` and `renderers/unified_dock.py` are both
Pillow-free. The options grid allocates a rect per name, so its count is
now total AND bounded (`MAX_CELLS`): a huge count can no longer become a
runaway allocation -- `10**9` cells OOM-killed the suite before that bound
existed.

Unescaping gained exactly one slot, and no caller can reach it.
`{{name|raw}}` is the ONE markup slot, filled only from a separate `raw`
mapping that the html renderer never passes -- so a caller value named
after a raw slot still arrives escaped. Raw slot names and values are
validated (a bad name, a non-string, or markup that will not render is an
error), and an unfilled raw slot is a missing variable that names itself
rather than drawing a hole. Every other `{{value}}` is escaped as before.
A failed render is one shared card (`renderers/_html_error.py`):
`error_frame` for a view that owns the panel, `error_strip` for a view
composited into a bigger frame, so a dock failure is a strip card and
never a full-screen card, and never a silent gap.

The 250-line gate is a real gate now rather than a baseline of known
exceptions. Nine files over budget were split into single-concept modules
(the largest, `renderers/beads_common.py` at 663 lines, became six), then
the last two: `displayd.py` (4396 lines) is the composition root, 203
lines composing `DisplayDaemon` from single-concept mixins and binding the
runtime globals the route mixins read; `touch.py` (1727 lines) is the CLI
entry point and re-export facade over thirteen single-concept modules. No
behaviour change: every moved function and class body is byte-identical to
its previous source -- `CONTROL_PAGE` still reassembles byte-for-byte from
its four parts -- every name another module imports still resolves, and
the documented monkeypatch contracts (`displayd.Framebuffer` /
`displayd.DAEMON` / `displayd.API_TOKEN`) are preserved by the composition
root binding them. The seven new Mac-side libs joined
`bridges/mac-set.manifest`, because the Mac set deploys as a unit.
`tests/test_repo_health.py` now asserts the green gate instead of the old
two-file exception.

One regression arrived with the migration and is fixed on its own
evidence. `FramesTest::test_empty_and_stale_differ_in_dock_only` compares
rendered dock pixels, and with the engine unbuilt empty and stale both
fall back to the same strip, so there was no difference to find: it failed
on any clean checkout. It now carries the file's own `@native_built` gate,
the one the other engine-dependent tests in that file already had. The
assertion is untouched -- wherever the engine is built, empty and stale
must still differ inside the dock rect -- and the two sibling tests stay
ungated on evidence rather than by omission: one checks frame size only,
and the other compares the grid zone, which the fallback strip cannot
reach.

Minor bump: how the home screen renders changed, and that change is
additive and backwards compatible -- no API and no view contract changed,
and every migrated surface keeps its existing touch geometry. One
bookkeeping note travels with it: `APP_VERSION` now lives in
`daemon_config.py`, where the split moved it, and `displayd.py` only
re-exports it, so a reader looking for the version line will not find it
in the composition root.

## 0.6.1

Deploying to the host works again. `deploy.sh`'s LiteHTML gate refused
before its restart step, so merged work stopped reaching the live panel
while the panel itself stayed up. That step held three defects; each one
on its own stops the deploy, and each was reproduced against the real host.

1. The reported one: the installer copies a file onto itself. deploy.sh runs
   `tools/install_html_runtime.sh --prefix ~/displayd` on the box, and in
   that configuration the installer's own repo root IS the prefix, so every
   artifact copy was `install <file> <same file>`. GNU install refuses those
   -- `install: '.../renderers/native/displayd_html.h' and
   '.../renderers/native/displayd_html.h' are the same file` -- and the
   script exited 1 before printing its completion token, which the 0.6.0
   gate reads as an unusable renderer. The engine was already built on the
   host; nothing was wrong with it.
2. The prefix never reached the installer at all. REMOTE_DIR's default is
   `~/displayd`, and the remote command was `sh '~/displayd/tools/...'`: a
   quoted tilde is literal for the local shell and for the remote one, so
   the step captured only `No such file or directory` (rc 127), saw no
   completion token, and refused. The self-copy error in (1) was invisible
   behind this one under the documented default invocation.
3. rsync shipped a Mac-built engine to the Linux host. rsync knows nothing
   about .gitignore, so a developer's checkout carried its Mach-O
   `liblitehtmlpil.dylib` across; `_html_native.LIB_NAMES` tries `.dylib`
   first, so that file shadowed the host's own working `.so` and the html
   view died on a host whose real engine was fine.

The installer's three copies now go through one `install_file()` helper
that skips a copy whose destination already IS the source, decided on
resolved identity (canonical path, or same device+inode) rather than string
equality -- so a relative or symlinked spelling of the prefix is handled
too -- and never skips when the paths cannot be resolved. deploy.sh hands
the remote shell a `$HOME`-relative prefix, still double-quoted there so a
remote dir with spaces survives, and its rsync list now holds back
`liblitehtmlpil.*` and `build/`.

The gate itself is unchanged: the installer's `complete:` token still
decides, `--strict` is still not passed, and a genuinely absent renderer is
still reported `incomplete`. Only the self-copy no-op and a path that never
reached the tool are now successes. Patch bump: no API and no view
behaviour changes -- the deploy path now does what it already documented.

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
