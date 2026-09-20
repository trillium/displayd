# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.

## Deploying to lnx-server

- Live host is `lnx-server` (tailnet `100.81.88.113`); ssh as `trillium@lnx-server`.
- Deployed copy lives at `~/displayd` (NOT `/opt/displayd`); `install.sh` was
  run with that prefix, so the unit file is the repo's `displayd.service` with
  the path rewritten plus a host-only `Environment=DISPLAYD_BIND=<tailnet-ip>`.
- The API has no auth: never bind `0.0.0.0`. Tailnet-only bind keeps the panel
  drivable from a MacBook while refusing LAN/localhost clients (host-local
  callers must use the tailnet address too).
- Restarting the daemon blanks the screen (`DisplayDaemon.clear()` on boot),
  so always `POST /show` afterwards; verify backlight value restores on power
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
- Priority: notice > chat-attention > idle-off; manual `/show`/`/clear`
  cancels every transient. Chat-attention and idle-off are OFF by default.
- Backlight restore is floored (task-i0agw): dimmed readings (<10% of max)
  are never saved, and a too-low restore target falls back to max.
- Policy config persists in `policy.json` next to the daemon
  (`DISPLAYD_POLICY` overrides); feed buffers do not persist.

## MCP + display feedback

- Agent entry point is `mcp_server.py` (stdlib-only MCP over stdio,
  `DISPLAYD_URL` env, no auth to configure); per-view/per-input tools are
  derived live from `GET /renderers`, so a new renderer file is new tools
  with no server change.
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
- Deploy is `rsync` of the repo to `~/displayd` (no git there), then
  `sudo systemctl restart displayd`. Chat is quiet late at night: an empty
  panel with "waiting for chat" is the healthy idle state, not a bug.

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
