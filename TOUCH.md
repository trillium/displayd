# displayd touch input (lnx-server jumbotron)

`touch.py` is a standalone, stdlib-only companion to `displayd` that makes
the panel tap-interactive. It reads a Linux touchscreen input device,
decodes evdev multitouch/pointer events, maps them to display pixels, and
invokes existing safe displayd HTTP actions when a tap lands in a configured
hit region. It never touches the framebuffer or renderer core -- the only
coupling to displayd is the public HTTP API.

Quick start (foreground smoke test)::

    python3 touch.py --list-devices
    python3 touch.py --device /dev/input/event8 --width 1920 --height 1080 --dry-run
    python3 touch.py --config touch.json

## Transport and trust boundary

- Transport is plain HTTP to the existing displayd API (`POST /playlist/next`,
  `/screen/on`, `/show`, `/notify`, ...). Default endpoint is loopback
  (`http://127.0.0.1:8980`); on lnx-server point it at the tailnet-bound
  displayd address instead.
- **The displayd API is unauthenticated by design.** `touch.py` does not add
  auth and does not need any: run it on the same host (loopback) or over the
  tailnet, never across the open internet. Do not bind displayd wider to
  accommodate touch -- point touch at displayd, not the other way round.
- **Caller rule (closed): the configured `endpoint` must be loopback
  (`127.0.0.0/8`, `::1`, `localhost`) or a tailnet address
  (`100.64.0.0/10`)**, else `load_config()` refuses it before the device
  is opened and `DisplaydClient.dispatch()` refuses it without sending a
  byte. LAN literals, public IPs, non-local hostnames (including tailnet
  MagicDNS -- use the tailnet IP literal), and non-http(s) schemes all
  fail closed. See "Named-action allowlist + caller rule" below.
- The action model is a closed allowlist (today: `playlist_next/pause/resume`,
  `screen_on/off`, `clear`, `show`, `notify`, `feedback`, `reload_confirm`). There is no generic
  "POST any path" or shell action, so a bad config cannot become command
  execution. No credentials live in source control; there are none to
  configure.

## Named-action allowlist + caller rule (parlay guard shape)

The structure mirrors trillium/parlay's chat guard
(`packages/server/src/guard/paths.ts`, `origin.ts`, `index.ts`):

- `paths.ts` owns WHICH routes are guarded -- a closed set classified by
  handler effect, with the accepted residue named. Its classification rule,
  quoted: the guarded set is "the routes that write server state, drive a
  device, or hand out an identifier the rest of the surface can then be aimed
  with. Within that surface, membership is decided by what the handler DOES,
  REGARDLESS OF HTTP METHOD."
- `origin.ts` owns WHO may call them (loopback / private-LAN / allow-list).
- `index.ts` applies the policy with silent denies (refusals carrying no CORS
  headers, so a refused caller learns nothing back).

Mapped onto touch (`touch.py`):

- **Action table (`ACTION_TABLE`)** = WHICH named actions may ever run. Each
  entry maps an action name to its handler effect: the displayd endpoint the
  tap drives plus the fixed body shape (the touch analogue of "what the
  handler does"). The table is closed by default: `resolve_action()` returns
  `None` for unknown names (logged, no HTTP, no exception in the service
  loop -- the index.ts silent-deny shape), while `action_request()` raises
  `ValueError` for the same input so `load_config()` fails fast on a bad
  config before the device is opened. `DisplaydClient.dispatch()` applies
  both halves at dispatch time and returns an error summary on denial.
- **Caller rule (`endpoint_allowed()`)** = WHO may be called: loopback or
  tailnet only. Deliberately stricter than parlay's `origin.ts` (which also
  admits private-LAN for the phone panel): touch has no LAN caller, so LAN
  literals fail closed here.
- **Named residue** (deliberately outside the table, recorded in `touch.py`
  next to `ACTION_TABLE`): no generic "POST any path" action, no shell-out
  action, no free-text feedback notes/params passthrough, no MagicDNS / LAN
  / public endpoint. Each stays out until a named action with a fixed body
  shape justifies it.

How to add a named action (all four, no shortcuts):

1. Add one `ACTION_TABLE` entry: name, one-sentence `effect` (the panel
   state it changes -- this is the classification), `method`/`path`, and
   the `params` it reads.
2. Validate its parameters in `_resolve()` in `touch.py`: required fields,
   types, and ranges (ratings are `int` 1-5, `bool` excluded); build a
   fixed-shape body and ignore everything else in the dict.
3. Add tests in `tests/test_touch.py`: valid dispatch, malformed payloads
   denied by both `action_request()` (raises) and `resolve_action()`
   (silent `None`), and an end-to-end region tap where it fits.
4. Document it here and add an example entry to `touch.json.example`.

The first action added under this table is `feedback`: a tap records a
fixed-shape panel rating -- `{"name": "feedback", "view": "clock",
"rating": 5}` posts `{"view", "rating", "agent": "touch"}` (plus an
optional `categories` list) to `POST /feedback`. The `agent` field is pinned
to `"touch"` so a tap cannot spoof authorship, and free-text notes/params
stay out per the residue rule above.

The second is `reload_confirm`: a tap confirms the showing reload view --
`{"name": "reload_confirm"}` posts the pinned body `{"via": "tap"}`
to `POST /reload/confirm`, with no parameters to smuggle. The daemon gates
on the view (no reload showing -> 409 miss, never a view change), so bind
it to a generous region -- e.g. the full screen in `touch.json.example` --
and taps elsewhere keep their normal actions. It is the tap half of the
scan-confirmed reload relay (see README "Reload confirmation"): scanning
the QR confirms via `GET /r/<token>`, tapping confirms via this action,
and the view still auto-returns on timeout either way.

## Device discovery

On the target host (the default `/dev/input/event8` is the lnx-server local
value for the `G2Touch Multi-Touch` panel -- it is NOT universal)::

    python3 touch.py --list-devices
    cat /sys/class/input/event8/device/name        # confirm panel name
    sudo evtest /dev/input/event8                  # watch raw events, tap panel

Pick the `eventN` whose name matches the touchscreen. If the number moves
across reboots, write a udev rule pinning a symlink (e.g.
`/dev/input/touchscreen`) and put that symlink in `device`.

## Event record size (64-bit Linux only)

`touch.py` reads `struct input_event` as 24-byte records (`EVENT_FORMAT
= "<qqHHi"`: two 8-byte `timeval` longs + type + code + signed value).
The kernel validates `read()` counts against its native record size, so a
shorter read fails with `EINVAL` instead of returning data.

32-bit kernels emit 16-byte records (4-byte `timeval` longs) and are NOT
supported by this build -- there is deliberately no runtime format
guessing, which would silently misframe the stream. If 32-bit support is
ever needed, it must be an explicit, tested target.

## Permissions

The input device is usually `root:input` `660`. Options (pick one):

- Run the service as a user in the `input` group:
  `sudo usermod -aG input <user>` (re-login after).
- Or ship a udev rule dropping a group-readable symlink, e.g.
  `/etc/udev/rules.d/99-touchscreen.rules`:

  SUBSYSTEM=="input", ATTRS{name}=="G2Touch Multi-Touch*", SYMLINK+="input/touchscreen", GROUP="input", MODE="0660"

## Calibration

Find the raw range with `sudo evtest` (the `ABS_X`/`ABS_Y` min/max lines) or::

    cat /sys/class/input/event8/device/capabilities/abs   # hex bitmap (harder)
    grep -r . /sys/class/input/event8/device/id/ 2>/dev/null

Then set in `touch.json`:

- `width`/`height`: real panel pixels (framebuffer size, e.g. from `fbset`).
- `calibration.x_max`/`y_max` (and `x_min`/`y_min` if nonzero).
  The lnx-server G2Touch panel reports native display pixels (`ABS_X`
  0-1920, `ABS_Y` 0-1080 -- read them with `EVIOCGABS`, not the hex bitmap),
  so its calibration equals the panel size. A wrong range (e.g. a 4095 gyro
  default) silently compresses every tap toward the origin -- all taps land
  in the top-left cells and the right/bottom of the screen is unreachable.
  Symptom check: mapped tap coordinates never exceed
  `device_max / x_max * (width - 1)`.
- Orientation: `swap_xy`, `invert_x`, `invert_y`, `rotation` (0/90/180/270).
- Verify with `--dry-run`: taps log `tap at X,Y -> region ...` without HTTP.
  Corners should report near `(0,0)` / `(width-1,height-1)`.

`touch.json` in the repo is a commented example -- copy and edit it on the
host; the shipped built-in default is two wide zones (right third =
playlist-next, left third = screen-on, middle = dead).

Every valid tap also sends `POST /touch/tap` first, which dismisses an
active reload confirmation (`POST /reload` stays up indefinitely until a
tap returns it to the base view, or the clock after a fresh restart).
Dismissal runs before region hit-testing, so even middle/dead-zone taps
dismiss reload while dispatching no region action; a failed dismissal never
blocks the configured region action that follows.

## Foreground smoke testing

    python3 touch.py --config touch.json --dry-run     # no HTTP, logs taps
    python3 touch.py --config touch.json -v            # verbose per-event

Expected log lines: `touch service starting`, `tap at X,Y -> region '...'`,
`tap at X,Y hit no region`. Ctrl-C stops cleanly (SIGTERM too).

## Touchscreen confidence mode (opt-in tap test)

A visible tap-test view for proving the panel mapping without guessing.
`renderers/touch_confidence.py` draws every configured touch region as a
labelled box (`id → action`) under a prominent title, plus live
diagnostics: last tap coordinates, matched region/action or dead-zone
result, total/hit/miss counters, and the action result/error. Every
resolved tap repaints the frame.

It is off by default and fully additive: with the switch absent or false,
touch actions and clock/playlist behaviour are byte-for-byte what they were
before confidence mode existed.

Enable (reversible, on lnx-server):

1. Show the view, passing the touch.json regions so the boxes match the
   live hit-test (rects are display pixels; `width`/`height` declare their
   coordinate space when it differs from the screen):

       curl -X POST http://100.81.88.113:8980/show \
         -d '{"renderer": "touch_confidence", "params": {
               "width": 1920, "height": 1080, "regions": [
                 {"id": "screen-on", "rect": [0, 0, 640, 1080],
                  "action": {"name": "screen_on"}},
                 {"id": "playlist-next", "rect": [1280, 0, 640, 1080],
                  "action": {"name": "playlist_next"}}]}}'

2. Turn on tap feedback in the touch service config (`touch.json`):

       "confidence_feedback": {"enabled": true}

   or without editing the file: `DISPLAYD_TOUCH_CONFIDENCE=1` in the
environment, or `python3 touch.py --confidence-feedback`. Then restart
   the touch service only (`sudo systemctl restart displayd-touch` --
   displayd itself is untouched). Every resolved tap -- region hit AND
   dead-zone miss -- is POSTed best-effort to
   `/feed/touch_confidence/tap` *after* the configured action dispatches,
   so feedback can never suppress or alter taps. Feedback failures are
   logged and swallowed. Swipes, long presses, incomplete events, and
   debounce-suppressed taps emit nothing.

3. Tap the panel: boxes, counters, and the last-tap line update live.
   The middle dead zone reports `DEAD ZONE` to the confidence feed and
   then routes to the options view (tap-anywhere fallback) -- that
   navigation is the way back; re-show the confidence view to resume
   testing. Set `tap_options.enabled: false` to restore dispatch-nothing
   dead zones while testing.

Disable (back to normal):

    curl -X POST http://100.81.88.113:8980/show \
      -d '{"renderer": "clock", "params": {}}'   # panel back to clock
    # then in touch.json: "confidence_feedback": {"enabled": false}
    # (or unset DISPLAYD_TOUCH_CONFIDENCE), restart displayd-touch.

Feeding the confidence view never pulls the screen: it is an ordinary feed
buffer, not a chat-attention event, so taps cannot interrupt the clock,
playlist rotation, or a layout -- they only repaint the confidence view
while it is shown.

## Reload tap-dismissal (every tap returns a reload view)

`POST /reload` stays up indefinitely until a touchscreen tap dismisses it
(`POST /touch/tap`, sent best-effort by `touch.py` before hit-testing on
every valid tap) or a manual `/show` cancels it. Dismissing anything but
an active reload is a server-side no-op (`dismissed: false`), so repeated
taps are safe; a real dismissal returns through the normal return path
(saved base view, else clock). The `reload_confirm` named action
(`POST /reload/confirm`) confirms the showing reload view via tap
(view-gated: 409 miss when no reload is showing).

## Retro grid wiring (4x3 arcade buttons)

`renderers/retro_grid.py` is a tappable 4x3 button grid (default labels
1-12, per-cell text or contain-fit image, arcade styling). Hit regions
live in `touch-retro-grid.json.example` -- 12 rects matching the default
1920x1080 layout, generated by `python3 renderers/retro_grid.py
--width 1920 --height 1080` (re-run for another W/H and paste under
`"regions"`). Each cell fires the existing allowlisted `notify` action
(transient `CELL N` notice, then automatic return); no new generic action
was added -- see "Named-action allowlist" above for how to add one.
In-grid flash (tapped cell inverts on the next frame, visible in
`/snapshot`) comes from the same `confidence_feedback` switch as above,
pointed at renderer `retro_grid`, input `tap` -- already set in the
example. Feed a tap by hand with `curl -X POST
http://100.81.88.113:8980/feed/retro_grid/tap -d '{"cell": 5}'`.
When merging the 12 cells with pre-existing regions (e.g. the playlist-next
and screen-on thirds), list the retro cells FIRST: `hit_test()` gives
earlier entries every overlap, and the full-height thirds otherwise shadow
the grid (they stay reachable in margins/gutters). Validate with
`touch.load_config()` before restarting the service.

## Tap anywhere: unconsumed taps route to options

A tap that hits no configured region -- the middle dead zone in the
shipped default -- is routed to the view-selection screen
(`renderers/options.py`) via the `options` named action
(`POST /show {"renderer": "options"}`), so every fullscreen view has a
tap path to a screen that names the way back. The fallback lives in the
shared input path (`TouchService.handle_frame`), never in per-renderer
code: it is view-agnostic, so it covers every built-in view with no
per-view wiring.

Precedence (highest wins):

1. **Reload dismiss** -- every valid tap `POST /touch/tap` first. When
   the daemon reports `dismissed: true`, the tap was consumed by that
   gesture: the panel is on its normal return path and the options
   fallback stays out. (Region hits still dispatch after a dismissal,
   exactly as before -- only the new navigation is gated.)
2. **Configured region hit** -- tap-to-rate `feedback`, `playlist_next`,
   `screen_on`, `reload_confirm`, and friends dispatch exactly as before.
   Existing gestures keep working; the fallback never fires for a tap a
   region consumed.
3. **Tap-anywhere fallback** -- dead-zone taps `POST /show` to the
   options view. The resulting manual selection cancels any transient in
   flight (`notice`, `reload` confirmation): a manual `/show` wins over a
   transient by daemon design, so a tap during a notice/reload screen
   lands options instead of being swallowed -- no view traps the user.
4. **Tap-test view** (`touch_confidence`) -- dead-zone taps feed the
   confidence diagnostics first (reported as dead zone) and then route to
   options like anywhere else. That navigation is the escape hatch back;
   re-`POST /show` the confidence view to resume testing.
5. **Options view itself** -- a tap while already there re-shows options
   (harmless no-op), so options never traps either.

Picking a view back happens on the phone-first control page (`GET /`
one-tap grid) or `POST /show` directly; `POST /playlist/resume` restarts
rotation. The options screen lists the pinned picks (clock, chat, row,
stream) plus that return path.

Configure (`touch.json`):

    "tap_options": {"enabled": true, "renderer": "options", "params": {}}

`false` (or `{"enabled": false}`) restores the old dead-zone behaviour
(log + optional confidence feedback, no navigation). `renderer` must be a
plain view name and `params` a plain object; the fallback dispatches
through the closed `options` action, so it inherits the fixed-shape body
and the loopback/tailnet caller rule -- a bad config fails fast in
`load_config()` before the device is opened.


## Service supervision (lnx-server)

Template unit: `touch-input.service` (review before installing -- the worker
does NOT install it). Typical deploy (separately reviewable step, on host):

    sudo cp touch-input.service /etc/systemd/system/displayd-touch.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now displayd-touch
    journalctl -u displayd-touch -f

The unit restarts on failure (`Restart=on-failure`) but a missing device at
boot exits 2 without restart storms -- check `journalctl` if it never starts.

## Rollback / disable

Touch is fully additive: displayd never depends on it.

    sudo systemctl disable --now displayd-touch   # stop taps having effects
    # displayd itself is untouched; output path never routes through touch.py

To re-verify the panel is output-only afterwards: `systemctl status
displayd-touch` shows inactive, and taps produce no log lines.
