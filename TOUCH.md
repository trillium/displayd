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
- The action model is a closed allowlist (`playlist_next/pause/resume`,
  `screen_on/off`, `clear`, `show`, `notify`). There is no generic "POST any
  path" or shell action, so a bad config cannot become command execution.
  No credentials live in source control; there are none to configure.

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
- Orientation: `swap_xy`, `invert_x`, `invert_y`, `rotation` (0/90/180/270).
- Verify with `--dry-run`: taps log `tap at X,Y -> region ...` without HTTP.
  Corners should report near `(0,0)` / `(width-1,height-1)`.

`touch.json` in the repo is a commented example -- copy and edit it on the
host; the shipped built-in default is two wide zones (right third =
playlist-next, left third = screen-on, middle = dead).

## Foreground smoke testing

    python3 touch.py --config touch.json --dry-run     # no HTTP, logs taps
    python3 touch.py --config touch.json -v            # verbose per-event

Expected log lines: `touch service starting`, `tap at X,Y -> region '...'`,
`tap at X,Y hit no region`. Ctrl-C stops cleanly (SIGTERM too).

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
