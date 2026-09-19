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
