# Changelog

All notable changes to displayd, newest first. Every change bumps
`APP_VERSION` in `displayd.py` (the single source of truth) per the
convention in README.md "Versioning": tiny to patch, medium to minor,
large/breaking to major.

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
