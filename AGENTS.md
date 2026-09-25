# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.

## Deploying to lnx-server

- Deliver with `./deploy.sh` (rsync + restart + re-show + `/reload` proof +
  `~/displayd/DEPLOYED` stamp + verify; panel-visible via `GET /deploy`,
  `GET /state`'s `"deploy"` key, and the control page). No `--delete`:
  host-local files (`touch.json`, `state/`, `backups/`, `policy.json`,
  feedback log) must survive redeploys.
- Live host is `lnx-server` (tailnet `100.81.88.113`); ssh as `trillium@lnx-server`.
- Deployed copy lives at `~/displayd` (NOT `/opt/displayd`); `install.sh` was
  run with that prefix, so the unit file is the repo's `displayd.service` with
  the path rewritten plus a host-only `Environment=DISPLAYD_BIND=<tailnet-ip>`.
- The API has no auth: never bind `0.0.0.0`. Tailnet-only bind keeps the panel
  drivable from a MacBook while refusing LAN/localhost clients (host-local
  callers must use the tailnet address too).
- Restarting the daemon blanks the screen (`DisplayDaemon.clear()` on boot),
  so always `POST /show` afterwards; to prove the new build instead, `POST
  /reload {"sha": "<full-40-char-sha>"}` shows a RELOADED +
  SHA + commit-QR screen that stays until a touchscreen tap (`POST
  /touch/tap`, auto-sent by `touch.py`) returns it to the prior view
  (rule + example in README "Reload confirmation"). Verify backlight value restores on power
  round-trips and never leave the panel black.
- Beads overview store colours/icons: edit `~/displayd/state/beads-stores.json`
  on the host (format in `renderers/beads_style.py` docstring); the panel
  reloads it live, no restart needed.

## Autonomous behaviour (policy layer)

- One module owns it: `policy.py` (activity clock, transient switch/return,
  idle-off decision, persisted config). Daemon actuators live in
  `displayd.py`; transient view is `renderers/notice.py`.
- API: `POST /notify`, `POST /feed/<renderer>/<input>`, `GET`/`POST /policy`
  (all on the control page too). Only mutating POSTs count as activity;
  `GET` polling never resets the idle clock.
- Priority: notice = reload (2) > attention (1); newest of the top pair wins.
  Manual `/show`/`/clear` cancels every transient. Chat-attention and idle-off are OFF by default.
- Reload QR rule: payload is always
  `https://github.com/trillium/displayd/commit/<full-sha>` (derived
  server-side in `renderers/reload.py` + `DisplayDaemon.reload()`; the
  request carries only the SHA, so no arbitrary QR URL is possible).
- Backlight restore is floored (task-i0agw): dimmed readings (<10% of max)
  are never saved, and a too-low restore target falls back to max.
- Policy config persists in `policy.json` next to the daemon
  (`DISPLAYD_POLICY` overrides); feed buffers do not persist.

## Control page (phone-first, GET /)

- One template owns it: `CONTROL_PAGE` in `displayd.py` (presentation
  only -- every control drives an existing API endpoint, no page-specific
  routes). Sections top-to-bottom: Views (one-tap grid, big four pinned:
  clock/chat/row/stream, current view highlighted) / Playback / Proof
  (reload + deploy stamp, also pinned in the sticky top bar) / Feedback
  (rate + summary) / Now showing / Notify / Policy / Tap actions.
- Guardrails in `tests/test_control.py` (`TestPhoneFirstRebuild`): no
  hardcoded renderer names, tap-action list matches `touch.ACTION_TABLE`,
  every fetched path has a daemon route. Verify against a live headless
  daemon (`DISPLAYD_FAKE_FB=1`) by curling each button's endpoint.

## MCP + display feedback

- Agent entry point is `mcp_server.py` (stdlib-only MCP over stdio,
  `DISPLAYD_URL` env, no auth to configure); per-view/per-input tools are
  derived live from `GET /renderers`, so a new renderer file is new tools
  with no server change.
- Feed health states live in `FeedStore` (`displayd.py`): cold/warm/stale/error
  (`classify_health`; a failed push marks error until a later push succeeds).
  `POST /show {"renderer": "feed_health"}` renders the dashboard
  (`renderers/feed_health.py`, auto-refreshes every 5s).
- Display feedback log is JSONL next to the daemon (`DISPLAYD_FEEDBACK`
  overrides) with per-note PNG frames in `<log>_frames/`; API is
  `POST`/`GET /feedback`, `GET /feedback/summary`, `GET /feedback/<id>`,
  `GET /feedback/<id>/frame`, plus `GET /feed/<view>/<input>` for feed
  health. Feedback never resets the idle clock.
- Headless testing: `DISPLAYD_FAKE_FB=1` selects the in-memory framebuffer
  double (CI/local demos only; never set on the host).

## Static-region composition (layout layer)

- Opt-in over the single-view core: `POST /layout {"regions": [...]}`,
  `GET /layout`, `DELETE /layout`; a bare `POST /show` or `/clear`
  exits layout mode. Geometry is stack (`height`/`width`, px or %), grid
  (`rows`/`cols` + `row`/`col`/`row_span`/`col_span`), or explicit `rect`;
  pure validator is `parse_layout()` in `displayd.py` (atomic reject).
- Each region runs its renderer in a `RegionScreen` thread; presents
  recomposite from the per-region frame cache, so one region updating
  never disturbs others. Renderer crashes are contained per region
  (last-good-frame kept, error in `/state` layout entry).
- Feeds stay global, so they route to whichever region(s) bind that
  renderer. Chat-attention pulls are suppressed while a layout is
  active; `POST /notify` still interrupts full-screen (clearing layout).
- Tests: `tests/test_layout.py` (parser, daemon, HTTP); headless via
  `DISPLAYD_FAKE_FB=1`.

## Chat view and Firebot bridge (v1)

- `renderers/chat.py` renders the rolling window; feed it with
  `POST /feed/chat/message` (`{author, text, ...}`) and retract with
  `POST /feed/chat/delete` (`{messageId}`). Schemas advertised in
  `GET /renderers`; `/state` shows feed health plus last-switch timings.
- Chat retention is persistent: both chat inputs declare `buffer: 0`
  (unbounded), so a message leaves panel state only on moderation
  delete -- never for age or count. The visible window stays
  screen-bounded (`lines` param, default 7) in the renderer.
- `bridges/firebot_chat.py` (stdlib only) subscribes to Firebot's overlay WS
  and pushes across the tailnet. The durable home is a Mac LaunchAgent,
  `com.displayd.firebot-chat-bridge` (plist template in `bridges/`, installed
  in `~/Library/LaunchAgents`, logs in `~/Library/Logs/`), pointed at
  `ws://127.0.0.1:7472` since Firebot runs on the MacBook. KeepAlive re-arms
  it after crashes; its own reconnect loop survives Firebot restarts. The
  older lnx-server systemd unit (`bridges/*.service`) is retired -- one
  writer only, or messages double-post.
- IMPORTANT: the installed plist points at the displayd checkout that holds
  the script. Repoint `ProgramArguments` to the merged main checkout path
  whenever the code moves worktrees, then `launchctl kickstart`.
- Deploy with `./deploy.sh` (no git on the host). Chat is quiet late at night:
  an empty panel with "waiting for chat" is the healthy idle state, not a bug.

## Playlist mode (rotation + progress bar)

- Scheduler lives in `playlist.py` (`Playlist` thread on top of
  `_start_view`); config is the `playlist` section of the policy surface
  (enabled/placement/thickness/direction/color/tick_seconds/views).
- Bar is composited via `Screen.overlay` (`overlay_image` hook) +
  `repaint_overlay()` tick; hidden whenever rotation holds (transient,
  screen-off, manual hold). Rotation never touches the activity clock.
- Colour precedence: per-view `color` > renderer `ACCENT` attr >
  playlist `color` > white fallback, always with a contrast border.
  A renderer declares `ACCENT = "#rrggbb"` to opt in (additive).
- Manual `/show`/`/clear` holds rotation until `POST /playlist/resume`;
  boot-time `clear()` is followed by `playlist.boot()` so a persisted
  `enabled: true` resumes after restart.
- Cache discipline: the frame cache (`Screen.on_present` hook) stores
  pre-overlay frames only, so a cached re-entry never serves a stale bar.

## Touch input (tap-to-action bridge)

- Companion `touch.py` (stdlib only) decodes evdev MT/single-touch from
  `/dev/input/event*` and POSTs existing safe displayd actions on taps in
  configured hit regions; daemon stays output-only, framebuffer path
  untouched. Operator doc is `TOUCH.md`, example `touch.json.example`,
  unit template `touch-input.service` (review paths/user before installing).
- `/dev/input/event8` is only the lnx-server local default (`G2Touch
  Multi-Touch`); always confirm with `touch.py --list-devices` + evtest,
  and set real `width`/`height` + raw `x_max`/`y_max` per panel.
- evdev `value` is signed (`<qqHHi`, 24-byte 64-bit records); packing it unsigned breaks
  `ABS_MT_TRACKING_ID -1` (lift). Synthetic-stream tests live in
  `tests/test_touch.py`; smoke live with `--dry-run` before enabling.
- Touch allowlist is guard-shaped (parlay `packages/server/src/guard/`
  mirror): `ACTION_TABLE` in `touch.py` (closed, classified by handler
  effect) + `endpoint_allowed()` (loopback/tailnet-CGNAT only) + silent
  denies at dispatch; first table action is tap-to-rate `feedback`.
  Procedure to add actions: TOUCH.md "how to add a named action".
- Tap anywhere → options: unconsumed (dead-zone) taps `POST /show
  {"renderer": "options"}` via the `options` table action + `tap_options`
  config (on by default); `renderers/options.py` is the on-panel selection
  screen. Precedence: reload-dismiss consumption > region hit > options
  fallback (a tap that actually dismissed reload never also navigates);
  manual `/show` cancels notice/reload transients, so no view traps the
  user. `tap_options.enabled: false` restores dispatch-nothing dead zones.
- Touchscreen confidence mode (opt-in tap test): `renderers/touch_confidence.py`
  draws the configured regions + live tap diagnostics; `touch.py`
  `confidence_feedback` switch (off by default, `DISPLAYD_TOUCH_CONFIDENCE=1`
  override) POSTs resolved taps best-effort to its feed after action
  dispatch. Reversible procedure + lnx-server calls in TOUCH.md;
  tests in `tests/test_touch_confidence.py`.
- Every valid tap sends `POST /touch/tap` before region hit-testing: it
  dismisses only an active `reload` view (saved base, else clock) and is a
  server-side no-op otherwise, so center/dead-zone taps still clear reload
  while region actions are unchanged. A failed dismissal never blocks the
  region action that follows.
- Reload QR is a scan relay, not the commit page: `POST /reload` answers
  a one-time `relay_url` (`GET /r/<token>`, tailnet bind only, else an
  explicit tap-only fallback) whose scan 302-redirects to the commit
  page and dismisses the view like a tap. Single-scan, dies with the
  view; no tracking beyond the confirm.

## Row view remote source (mini1 PM5 feed)

- `renderers/row.py` sources the streak live from mini1's PM5 bridge WebSocket
  (`ws://mini1:8765/obs/ws`, the same socket OBS uses -- never a second
  serving path), via a stdlib-only WS client (`fetch_ws_stats`). Reachable
  from lnx-server over the tailnet; `source` param also takes an http(s)
  rows.txt URL or a file path, `path` stays the local rows.txt fallback.
- Remote sightings journal one ISO day each to a local JSON journal
  (`journal` param / `$DISPLAYD_ROW_JOURNAL` / next to the local log); streak
  math runs over union(local log, journal) with one row per journal day so
  the bank never inflates. Frozen-feed repeats are ignored via the persisted
  last sample. Unreachable remote keeps the last-known streak with a STALE
  marker (amber dot + footer tag), never blank.
- Tests: `tests/test_row.py` (`TestSourceParams`, `TestSighting`,
  `TestJournal`, `TestFetchers` with a fake WS server, `TestPollFallback`,
  `TestStaleRender`); existing `run()` tests pin explicit
  `source`+`journal` so the suite stays hermetic (no tailnet dependency).

## Stream monitor (jumbotron live frames)

- `renderers/stream.py` (`STATIC = False`): fullscreen latest-frame view at a
  capped fps (`fps` param, 0.5-5, default 2). Push wins over poll: feed
  `POST /feed/stream/frame` (`{data}` base64 or `{url}`, `buffer: 1` so a
  slow panel drops stale frames) beats the `url` snapshot-poll fallback.
- Headless ceiling measured in `tests/test_stream.py` (~4.9fps at the 5fps
  cap, 1920x1080, ~25ms/present): full-rate video is out of scope for the
  tile path by design, hence the cap. Frame source (Mac-side OBS snapshot
  server) is obs-agent territory; contract lives in the renderer docstring.
- Screenshot poller: `bridges/obs_poll.py` (stdlib only) grabs
  `GetSourceScreenshot` over obs-websocket v5 on a 0.5..5 fps cadence and
  POSTs `{data}` to `/feed/stream/frame`; password via `OBS_PASSWORD` env
  only. Tests in `tests/test_obs_poll.py`; live checklist in its docstring.
- Start/stop is one action: `POST /show {renderer: stream, params: ...}` /
  bare `/show` or `/clear`. No auth/header params exist on purpose: serve
  snapshots tailnet-bound, unauthenticated, with no keys in files or logs.

## Webhook self-deploy (push-to-main redeploys the panel)

- Chain: GitHub push → funnel `https://vps01.hippo-tilapia.ts.net/hooks/displayd`
  → vps01 forwarder `127.0.0.1:8090` → lnx receiver `100.81.88.113:9898`
  → local deploy (fetch + reset `~/displayd-upstream`, rsync live tree,
  restart daemon, re-show, `/reload` proof, stamp, verify).
- Code home is `hooks/` (receiver, forwarder, both systemd units,
  `RUNBOOK.md`); deploy.sh rsync ships it to `~/displayd` automatically.
  vps01's copy at `/root/displayd-funnel/` is hand-copied (no checkout there).
  Retired proof logger kept at `/root/funnel-proof/hook.py` for restore.
- Receiver is a separate unit (`displayd-webhook.service`, root, secret in
  `/root/displayd-webhook-secret`), not a daemon route, so the mid-deploy
  restart can't kill it. Only main-branch pushes deploy; ping/other refs
  ack without action. Install/rotate/restore: `hooks/RUNBOOK.md`.
