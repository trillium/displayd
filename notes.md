# Coalescing the displayd UI around a component layer

Working notes for the overnight run. **Updated each increment;** the sections
state what exists now, what is deliberately left, and why.
`docs/HTML_RENDERER.md` owns the template contract, `AGENTS.md` the
project-intrinsic notes.

## What exists after this increment

### 1. The token layer — `renderers/theme.py`

The one module that owns colour, as semantic roles rather than per-view
constants: surfaces (`page`, `band`, `panel`, `pane`, `edge`, `rule`,
`badge`), type (`ink`, `ink-strong`, `ink-soft`, `muted`, `muted-soft`,
`faint`), state (`accent`, the four-colour `alert` family, `attention`,
`ok`), and identity (`ACCENT_SLOTS`, one accent per view name).

- **Templates** receive it as CSS custom properties.
  `_html_templates.load()` calls `_html_compose.compose()`, which splices
  one generated `<style>:root{...}</style>` block into the document head.
  The pinned litehtml revision implements css-variables-2, so
  `color: var(--ink)` resolves; an *undefined* name silently drops the
  declaration, which is why `tests/test_theme.py` proves resolution on
  rendered pixels rather than trusting the text.
- **Pillow** imports `theme` and reads `theme.rgb("<role>")`;
  `rgb()` is the only hex-to-tuple conversion point.

### 2. The component layer — `renderers/ui/`

The layer that owns the panel's pixels and is the only place a Pillow
drawing primitive may appear (rule enforced by the gate below).

- `renderers/ui/__init__.py` — what the layer is and the obligations of a
  component: one definition, every colour from `theme`, and **return the
  frame unchanged on any failure**.
- `renderers/ui/base.py` — `chain(*layers)`, the composition primitive
  promoted out of the old home-chrome module. It applies each layer in
  order and skips a layer that raises or returns None, so one broken
  component can never blank the panel. A single component gets the same
  guarantee by keeping the "return the frame unchanged" obligation.
- `renderers/ui/system_buttons.py` — the **first component**: the home
  badge and the sleep badge, one definition instead of two modules.

  `home_chrome.py` (201 lines) and `sleep_chrome.py` (144 lines) are
  deleted. They each carried a tile fill, a glyph, a strip width, a rect,
  a region, a draw function and a suppression list — two copies of "a
  badge", because nothing owned that concept. One module now owns
  `_paint_tile` (the shared dark rounded tile + outline, from the palette)
  and only the glyph differs (`_house` / `_moon`); `STRIP = 160` is one
  constant for both corners; `HOME_SUPPRESSED` / `SLEEP_SUPPRESSED` are
  two tables in one place; `audit_exact(view)` returns exactly the badges
  that are DRAWN, in composited order.

  Composition entry point: `system_overlay(screen, base_layer=None)`,
  which chains the playlist progress bar and the buttons through
  `ui.base.chain`. `daemon_core.py` now wires one dependency
  (`system_buttons_module.system_overlay(self.screen, self.screen.overlay)`)
  instead of three chained calls across two modules.

### Which views migrated, which are left

Migrated: the persistent overlay chrome (both system buttons) — the
always-on furniture that every view wears. It is the highest-leverage
component because it is the one surface that is *not* a view, and because
folding the pair removes a duplicate by construction rather than by
convention.

Deliberately left (still Pillow, still drawing by hand): the other 24
modules in the gate's exemption list — `beads`, `row`, `resources`,
`services`, `macbook`, `qr`, `reload`, `stream`, `activity`, `clock`,
`notice`, `text`, `retro_grid`, `touch_confidence`, `sleep`,
`feed_health`, `life`, the `*_draw` helpers, `playlist`/`playlist_bar`
(daemon-side progress bar), and `_html_error`/`_html_native`. Reason: the
component vocabulary they need (`tile`, `panel`, `stat`/list row, `shell`)
does not exist yet, and inventing it view-by-view would re-create the
duplication this run is deleting. The gate holds the line meanwhile.

### 3. The structural gate — `tools/check-components.py`

Rule: a module in scope that mentions `PIL.ImageDraw` is drawing by hand.
Scope: every `*.py` at the repo root (daemon-side panel UI: the playlist
progress bar lives there) plus every `*.py` under `renderers/`
recursively, **except** the component layer `renderers/ui/`. `tests/`,
`tools/`, `bridges/`, `hooks/` and build output are out of scope.

The migration is in flight, so the rule is a **ratchet**, not a switch:
`EXEMPTIONS` lists exactly the files that still draw by hand, and the gate
fails in both directions —

- a drawing file that is NOT exempt → fail, file named (new hand-drawing
  code cannot land);
- an exempt file that is gone, moved, or no longer mentions `ImageDraw` →
  fail (a stale exemption cannot hide a finished migration, and a renamed
  file has to re-earn its exemption).

so the list can only ever shrink. `--list` prints the current offenders to
paste in after a migration; `--root PATH` inspects another tree, which is
what `tests/test_components.py` uses to exercise both failure directions.
Wired into `tools/check-repo-health.py` as step 3.

24 exemptions remain (the list is in the tool, sorted).

## Adding a new view from the layer

1. Give the view a module under `renderers/` as today — `run(screen,
   params, stop)`, plus `NAME`, `DESCRIPTION`, `PARAMS`, `INPUTS`,
   `STATIC`, and (once declared) the full/partial capability.
2. Take every colour from `renderers/theme.py`. A `#rrggbb` in a view is a
   defect; in a migrated template `tests/test_theme.py` fails on one.
3. Draw through the component layer:
   - a template view renders a named template from `html-templates/`; the
     token block reaches it automatically, and the shared chrome bands
     come from `layout.html` (see "still owed" for the include step);
   - a Pillow view needs a component in `renderers/ui/` — and if the
     component it needs does not exist, that is the next component to
     write, not a reason to draw by hand.
4. Never let a draw raise: return the frame unchanged. If the view
   composes several layers, compose them with `ui.base.chain`.
5. Remove the view's path from `EXEMPTIONS` in
   `tools/check-components.py`; the gate then fails if it ever starts
   drawing by hand again. Run `python3 tools/check-components.py` and
   `python3 tools/check-repo-health.py`.

## Layout presets and the full/partial vocabulary — NOT YET BUILT

Planned shape (nothing below exists yet):

- presets on top of the existing `POST /layout` grammar (`daemon_layout.py`,
  `parse_layout`, `RegionScreen`, `MAX_LAYOUT_REGIONS = 16`), not a second
  layout system: `full` (one view owns the screen — today's behaviour),
  `split-50-50` (two equal rows), `split-50-50-columns` (two 50-wide
  columns), `15-70-15` (narrow left band, wide centre, narrow right band;
  the centre is the primary and the side bands carry applicable
  applications, tappable to open them).
- a renderer-declared capability surfaced through `GET /renderers` — e.g.
  `FULL` / `PARTIAL` / `PRIMARY` — so a preset is offered only the views
  that fit it and a full-screen-only view cannot be dropped into a 70%
  column. Today nothing declares whether a view can render reduced: the
  only related thing is the "one complete frame, one swap" drawing
  discipline, which is about *how* a view draws, not what it supports.

Reason it is not in this increment: the presets need views that can render
into a sub-rect, which needs the component vocabulary (`panel`, `tile`,
`stat`) that the current increment only started. Building the presets
first would mean a layout system over views that still hand-draw
full-screen frames.

## Still owed (with the reason)

- **Template chrome composition** (`_html_compose` docstring: the include
  step for one shared chrome partial). litehtml has no `@import` and a
  template cannot inherit, so the include has to be expanded at load time
  in Python. The token block proved the compose step and its test story
  (`strip_tokens`) first.
- **The remaining components** (`shell`, `tile`, `panel`, `stat`) and the
  migration of the views listed above.
- **Layout presets** and the **full/partial capability declaration**
  (shape above).
- **PR**: this run's branch is pushed by the orchestrator; the PR itself
  has not been opened from here.

## Recorded evidence

Full objective suite (the stop-condition command):

    $ python3 tools/check-components.py && python3 -m unittest tests.test_picker \
        tests.test_unified tests.test_chat tests.test_html \
        tests.test_html_runtime_install tests.test_control tests.test_options \
        tests.test_layout && python3 tools/check-lines.py
    component layer ok: 24 shipped module(s) still draw by hand; all exempt, none stale
    Ran 304 tests in 51.246s
    OK
    line budget ok: all source files within 250 lines
    rc=0

    $ python3 tools/check-repo-health.py      -> rc=0
    $ python3 -m unittest discover -s tests -t .
    Ran 1321 tests ... FAILED (failures=2, errors=1, skipped=10)
      the only failures are the three known-red modules on this box:
      tests.test_mac_zoom, tests.test_deploy_reload_proof, tests.test_talon_apps

    $ python3 -m unittest tests.test_components -v      -> Ran 15 tests, OK

Real headless render (`DISPLAYD_FAKE_FB=1`, real daemon over HTTP on an
ephemeral port, temp policy/feedback/deploy paths), template view with both
system buttons composited over it **through the new component layer**:

    POST /show {renderer: html} -> 200
    state: renderer=html layout=None html.ok=True
    frame size: (1920, 1080)
    home        badge-fill=True glyph=True
    sleep       badge-fill=True glyph=True
    page        (7, 8, 12) present=True
    band        (11, 13, 19) present=True
    rule        (36, 64, 92) present=True
    accent      (127, 209, 255) present=True
    saved: /tmp/panel-system-buttons-1920x1080.png

So the badges are drawn by `renderers/ui/system_buttons.py` (the only
module that may), from `theme`, on top of a litehtml template view, in the
same frame.

## Note on the stop condition

The command above exits zero, but that is a **floor, not the finish line**:
the gate is a ratchet with 24 exemptions, and the layout presets, the
full/partial capability and the template chrome composition are still
owed. The stop condition became reachable because the gate exists and the
health gate stays green while the migration is in flight — which is
exactly what it was designed to allow.
