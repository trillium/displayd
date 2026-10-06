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

### 3. The shared chrome, composed at load time — `html-templates/_chrome.html`

**litehtml has no `@import` and a template cannot inherit from another**, so
the shared chrome used to be copy-pasted into every template that wore it:
98 lines of CSS across four templates, the same `.frame`/`.strip`/`.head`/
`.title`/`.rule`/`.foot` rules restated 8-14 times per file, held in
agreement by a test rather than by the renderer.

It is now authored **once**, as named sections of one partial, and spliced
into a template at load time by `_html_compose.expand()`:

    <!--#include css-->          the chrome stylesheet, inside <style>
    <!--#include head-->         the header band markup (eyebrow, status)
    <!--#include title-->        the title band
    <!--#include subtitle-->     one dim line under the title
    <!--#include rule-->         the rule under the title
    <!--#include foot-->         the footer band (label, label)
    <!--#include strip-left-->   the left gesture strip
    <!--#include strip-right-->  the right gesture strip

**Measured:** the four surfaces' stylesheets went from 98 lines of CSS to 19
(picker 25→4, options 24→3, chat 26→12, layout 23→0) plus ONE chrome
definition of 43 lines; the chrome rules restated per template went 8/11/9/14
→ 0/0/0/0.

A partial is `_`-prefixed: `available()` never lists it, `load()`/`source()`
refuse the name, and it ships by the same `html-templates/*.html` install
rule as the templates, so it travels with them. Failures are loud, because a
silently missing band is a plausible wrong panel: an unknown section, a
missing partial, an include cycle (depth-bounded), or a composed document
over 512KiB is a `TemplateError` the view draws as a card.

**The per-surface differences are data, not copies.** A class on `<body>`
selects a variant whose custom properties are the whole difference:
`panel` (the shell), `panel center` (options), `panel tight` (picker),
`panel wide` (chat), plus `bright` for the accent rule. The variants live in
the partial next to the rules, so the numbers have one home.

### 3b. A landmine this step uncovered (recorded in docs)

Three litehtml behaviours decide whether a chrome works, all found by
rendering pixels rather than by reading CSS:

1. **A shorthand whose value contains `var()` is dropped entirely.**
   `padding: 44px var(--inset) 0 var(--inset)` pads *nothing*, silently. Every
   inset in the chrome is a longhand for this reason, and
   `tests/test_html.py` fails on a shorthand carrying a `var()`.
2. **A rule using `var()` beats an inline `style` on the same element**, so a
   caller's `background`/`color` param has to override the *role*
   (inline `--page` / `--ink` on `<body>`), not the property.
3. `gap` is still dropped on a growing flex row (unchanged, documented).

### Which templates migrated, which are left

Migrated to the composer: `layout.html`, `picker.html`, `options.html`,
`chat.html` — every surface that draws a panel chrome band.

Left, each with a reason:

- `dock.html` — a strip in its own DESIGN-sized document, composited under the
  home screen; it has no chrome bands to share.
- `status.html` — the copy-me example: it shows the minimal template contract
  (tokens + variables), not the shell.
- `picker.html`'s **tile layer** still has two colour literals
  (`#120c20` tile ink, `#5a4670` shadow). They belong to the `tile` component
  that does not exist yet, so picker is deliberately NOT in the
  "no colour literal" test list (`tests/test_theme.py`): adding a token only
  picker means, or hiding the gap, would both be worse.

### 4. The structural gate — `tools/check-components.py`

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
   `STATIC`, and `CAPABILITY` when it can render into a region smaller
   than the panel (`partial`, or `primary` if it may be a preset's centre;
   omitted means `full`, and `POST /layout` will refuse it in a band).
2. Take every colour from `renderers/theme.py`. A `#rrggbb` in a view is a
   defect; in a migrated template `tests/test_theme.py` fails on one.
3. Draw through the component layer:
   - a template view renders a named template from `html-templates/`. The
     token block reaches it automatically. If it draws panel chrome, it
     includes the shared sections (`<!--#include css-->` and the band it
     wants) and names a variant on its `<body>`: the chrome is authored once
     in `_chrome.html`, and `tests/test_html.py` fails if the template
     restates one of its rules or invents a variant;
   - a Pillow view needs a component in `renderers/ui/` — and if the
     component it needs does not exist, that is the next component to
     write, not a reason to draw by hand.
4. Never let a draw raise: return the frame unchanged. If the view
   composes several layers, compose them with `ui.base.chain`.
5. Remove the view's path from `EXEMPTIONS` in
   `tools/check-components.py`; the gate then fails if it ever starts
   drawing by hand again. Run `python3 tools/check-components.py` and
   `python3 tools/check-repo-health.py`.

## Layout presets and the capability vocabulary — built this increment

### 5. `capability.py` — how much panel a view can render into

One small vocabulary, declared in the renderer's own module next to
`NAME`:

    CAPABILITY = "partial"

| term | meaning |
| --- | --- |
| `full` | full panel only. **The default**: an undeclared or misspelled value is `full`, because guessing a view can be reduced is how a fullscreen frame lands in a 288px band. |
| `partial` | renders into a reduced region (a split half, a side band) as well as the whole panel. |
| `primary` | `partial`, and may hold a preset's primary region — the centre of the bands, where the view carrying the work sits. |

Declaration flows one way and has one home per step:

- `renderer_registry.load_renderers` normalizes it onto the entry;
- `GET /renderers` publishes it (`"capability"`), so the picker and MCP
  see it with no extra call;
- `layout.parse_layout` is the sole authority on fit, through
  `capability.check_fit`: a declared `full` view in a region smaller than
  the panel is rejected by name (400 over HTTP), atomically, before
  anything on screen is disturbed;
- `capability.offered(renderers)` is what a layout slot may be *offered*:
  loaded, needs no params, and declared reduced-capable.

Declared `partial` today (verified by a real reduced render): `picker`,
`options`, `chat`, `html`, `clock`, `text`, `solid`. Declared `primary`:
`row` — the objective's “Talon fits that” centre. Everything else is
undeclared, i.e. `full`; 8 of 25 views are reduced-capable.

An entry built by hand (tests, embeddings) with no `capability` key is
trusted as flexible — the registry is where a loaded view's declaration
lives, so the parser stays usable on synthetic trees without loosening
the rule for shipped views.

### 6. `layout_presets.py` — named styles over the existing grammar

A preset is **not** a second layout system: it is a named set of region
specs *in* the `POST /layout` grammar (`layout.py`: stack, grid, rect)
plus the rule for which views each slot may carry.

    POST /layout {"preset": "15-70-15"}
    POST /layout {"preset": "15-70-15", "views": {"center": "row"}}

| style | slots (geometry) | rects at 1920x1080 |
| --- | --- | --- |
| `full` | `view` (no geometry — one region owns the panel) | (0,0,1920,1080) |
| `split-50-50` | `top` 50% height, `bottom` 50% | (0,0,1920,540), (0,540,1920,540) |
| `split-50-50-columns` | `left` 50% width, `right` 50% | (0,0,960,1080), (960,0,960,1080) |
| `15-70-15` | `left` 15%, `center` 70%, `right` 15% | (0,0,288,1080), (288,0,1344,1080), (1632,0,288,1080) |

Slot roles decide the defaults: the primary slot prefers a
`primary`-capable view (falling back to the best reduced one), a
navigation band always prefers `picker` — the tappable application list,
which is what “the side bands carry the applicable applications, tappable
to open them” means here — and a plain slot takes the best-fitting view
not already used, so a bare `split-50-50` does not put one view in both
halves. A navigation band is handed `params.views` = the applicable set
(`capability.offered`), so it lists what fits the band rather than every
advertised view.

`GET /layout/presets` is the read-only projection: per style, its slots,
their geometry, the default view, and the views each slot accepts. That is
the “offered per style, not unconditionally” surface; `GET /layout` now
reports the style a live layout came from (`"preset"`).

### 7. The system buttons over a layout (a hole this increment closed)

A layout composite was presented straight to the framebuffer, bypassing
`Screen.overlay`, so **the always-on badges vanished the moment a layout
owned the panel** — while their touch regions stayed live and announced.
`daemon_layout._overlaid()` now runs the layout composite through the same
overlay chain as a single view (before `fb.present`, inside the present
lock, after the generation check). Verified by pixels in layout mode, and
pinned by `test_system_buttons_stay_drawn_over_a_layout`, which fails on
the pre-change tree (red instead of the badge fill at the badge tile).

## Still owed (with the reason)

- **The remaining components** (`shell`, `tile`, `panel`, `stat`) and the
  migration of the views listed above. The `tile` component is the next one
  with a concrete pull: `picker.html`'s tile ink is still a literal.
- **Per-view colour constants** — `ACCENT = "#rrggbb"` still lives in
  ~12 renderer modules, and `playlist_color.accent_for` reads a renderer's
  `ACCENT` attribute by name. Folding that into `theme.ACCENT_SLOTS` (the
  table already exists) is the rest of the token migration, and it is a
  behaviour change, so it wants its own increment and its own test.
- **The control page does not offer the styles yet.** `GET /layout/presets`
  publishes them and `POST /layout {"preset": ...}` applies them, but the
  phone page has no style picker and no per-slot view selects. That is the
  operator surface for the “offered per style” rule and the next UI step.
- **A narrow-band application column.** `picker` in a 288px band reflows but
  is cramped (3 columns of ~69px tiles); a `cols` param (or a dedicated
  narrow band renderer) is the fix, and it belongs with the `tile`
  component. The presets are already honest about it: the band is the
  tappable application list, just not yet a pretty one.
- **Layout-mode taps.** While a layout owns the panel the touch service
  evaluates global regions only (view-scoped regions are skipped), so a
  band's tiles need global `touch.json` entries at the band geometry
  (`picker.picker_regions(w, h, views, rect=<absolute band rect>)`
  produces them). Not wired into a shipped config yet.
- **PR**: this run's branch is pushed by the orchestrator; the PR itself
  has not been opened from here.

## Recorded evidence

Full objective suite (the stop-condition command), after this increment:

    $ python3 tools/check-components.py && python3 -m unittest tests.test_picker \
        tests.test_unified tests.test_chat tests.test_html \
        tests.test_html_runtime_install tests.test_control tests.test_options \
        tests.test_layout && python3 tools/check-lines.py
    component layer ok: 24 shipped module(s) still draw by hand; all exempt, none stale
    Ran 326 tests in 52.357s
    OK
    line budget ok: all source files within 250 lines
    rc=0

    (with tests.test_layout_presets added to the same run: Ran 351 tests, OK)

    $ python3 tools/check-repo-health.py      -> rc=0
    (line budget, no tracked generated artifacts, component layer)

    $ python3 -m unittest tests.test_components -v      -> Ran 15 tests, OK
    $ python3 -m unittest tests.test_layout_presets -v  -> Ran 25 tests, OK

    Wider sweeps this increment (the three known-red modules on this box are
    never run as a gate: tests.test_mac_zoom, tests.test_deploy_reload_proof,
    tests.test_talon_apps):

    $ python3 -m unittest tests.test_theme tests.test_components \
        tests.test_policy tests.test_playlist tests.test_feed \
        tests.test_feed_health tests.test_auth tests.test_mcp \
        tests.test_touch_audit tests.test_touch_resolve \
        tests.test_home_chrome tests.test_repo_health \
        tests.test_deploy_html_step
    Ran 234 tests in 74.263s -> OK

    $ python3 -m unittest tests.test_row tests.test_stream tests.test_sleep \
        tests.test_qr tests.test_reload tests.test_beads tests.test_beads_* \
        tests.test_resources_services tests.test_retro_grid \
        tests.test_command_longpoll tests.test_feedback tests.test_version \
        tests.test_deploy tests.test_mac_install tests.test_obs_poll \
        tests.test_touch_confidence tests.test_touch tests.test_macos_state \
        tests.test_mac_preview_plan tests.test_webhook_receiver
    Ran 636 tests in 62.750s -> OK (skipped=10)

    (a full `discover` was started and deliberately stopped rather than left
    running; the two sweeps above plus the stop-condition set cover every
    module this increment touches, and the macbook/talon suites are the slow
    ones that bind external state.)

### Every layout style at 1920x1080 (this increment)

`python3 /tmp/layout_style_evidence.py` — real daemon on a loopback
ephemeral port, `DISPLAYD_FAKE_FB=1`, temp policy/feedback paths. Each
style: `POST /layout {"preset": <style>, "views": {...}}`, wait for every
region's first frame, `GET /snapshot`, then per-region colour count and the
badge pixels:

    daemon: DISPLAYD_FAKE_FB=1 http://127.0.0.1:61464

    full                 POST /layout preset=full -> 200
      view    clock    rect=(   0,   0,1920,1080) colours=    3 error=None
      preset=full first_pixel_ms=4.9 frame=(1920, 1080)
      badges: home True  sleep True

    split-50-50          POST /layout preset=split-50-50 -> 200
      top     clock    rect=(   0,   0,1920, 540) colours=    3 error=None
      bottom  picker   rect=(   0, 540,1920, 540) colours= 3242 error=None
      preset=split-50-50 first_pixel_ms=3.7 frame=(1920, 1080)
      badges: home True  sleep True

    split-50-50-columns  POST /layout preset=split-50-50-columns -> 200
      left    clock    rect=(   0,   0, 960,1080) colours=    3 error=None
      right   options  rect=( 960,   0, 960,1080) colours= 1128 error=None
      preset=split-50-50-columns first_pixel_ms=3.4 frame=(1920, 1080)
      badges: home True  sleep True

    15-70-15             POST /layout preset=15-70-15 -> 200
      left    picker   rect=(   0,   0, 288,1080) colours= 1904 error=None
      center  clock    rect=( 288,   0,1344,1080) colours=   77 error=None
      right   options  rect=(1632,   0, 288,1080) colours=  795 error=None
      preset=15-70-15 first_pixel_ms=10.8 frame=(1920, 1080)
      badges: home True  sleep True

    html template + system buttons: 200 (1920, 1080)
      home  badge fill=True glyph=True
      sleep badge fill=True glyph=True

    GET /layout/presets -> 200 styles: ['full', 'split-50-50',
      'split-50-50-columns', '15-70-15']
    GET /renderers capabilities: [('chat', 'partial'), ('clock', 'partial'),
      ('html', 'partial'), ('options', 'partial'), ('picker', 'partial'),
      ('row', 'primary'), ('solid', 'partial'), ('text', 'partial')]
    declared reduced: 8 of 25 views

Notes on that output: `clock` reports 3 colours because this box has no
DejaVu font, so the view falls back to the tiny default bitmap font at the
top-left (the same 3 colours in single-view mode — a font-environment
artifact, not a layout defect); the template views in the same frames carry
thousands. Every style painted its first pixel in 3-11ms (budget: 100ms) and
no region reported an error. The badge pixels in layout mode are the hole
this increment closed (see §7); before the fix every style printed
`badges: home False sleep False`.

Frames saved: `/tmp/preset-{full,split-50-50,split-50-50-columns,15-70-15}-1920x1080.png`
and `/tmp/preset-html-system-buttons-1920x1080.png`.

### The badge-over-layout rule fails before, passes after

    $ python3 -m unittest \
        tests.test_layout.LayoutDaemonTestCase.test_system_buttons_stay_drawn_over_a_layout
    pre-change:  AssertionError: Tuples differ: (255, 0, 0) != (13, 17, 28)
                 FAILED (failures=1)
    post-change: ok

The assertion is the badge tile pixel against `theme.rgb("badge")` at a
point inside the tile but outside the glyph, so it pins the drawn badge to
the palette role as well as to the layout composite.

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

### One chrome definition, four surfaces (this increment)

`python3 /tmp/chrome_render_evidence.py` (the script a rerun can rebuild from
the two calls below) — real daemon on a loopback ephemeral port,
`DISPLAYD_FAKE_FB=1`, temp policy/feedback/deploy paths, each surface through
its own renderer: `POST /show {"renderer": "html", "params": {"template":
"layout.html", "vars": {...}}}`, `POST /show {"renderer": "picker"|
"options"|"chat"}`, then `GET /snapshot` per surface:

    daemon: DISPLAYD_FAKE_FB=1 127.0.0.1:60699
    POST /show html template=layout.html -> snapshot 34051 bytes (view=html)
    POST /show {renderer: picker}        -> snapshot 147394 bytes (view=picker)
    POST /show {renderer: options}       -> snapshot  60847 bytes (view=options)
    POST /show {renderer: chat}          -> snapshot  36217 bytes (view=chat)
    chrome sections: css, foot, head, rule, strip-left, strip-right, subtitle, title
    layout   (1920,1080) strip(80,540)=(11,13,19) page(960,540)=(7,8,12)  eyebrow_accent=603  page=1689924 band=303034
    picker   (1920,1080) strip(80,540)=(11,13,19) page(960,540)=(18,12,32) eyebrow_accent=1146 page=0       band=323723
    options  (1920,1080) strip(80,540)=(11,13,19) page(960,540)=(8,10,16)  eyebrow_accent=7034 page=0       band=303121
    chat     (1920,1080) strip(80,540)=(10,10,14) page(960,540)=(10,10,14) eyebrow_accent=7418 page=0       band=0
    system button home  at [0,0,160,160]:     fill=True glyph=True
    system button sleep at [1760,0,160,160]:  fill=True glyph=True
    saved: /tmp/chrome-evidence-*/panel-{layout,picker,options,chat}-1920x1080.png

`band` (11,13,19) on the three strip surfaces is the chrome's `.strip` rule,
`accent` on all four is the chrome's `.eyebrow`, and chat's `band=0` is
correct -- it has no gesture strips. The badge fill/glyph on the layout frame
is the system-button component over a template view.

### The anti-drift rule fails before, passes after

    chrome-owned selectors (22): *, .band, .body, .eyebrow, .foot, .foot .l,
      .foot .r, .frame, .head, .lead, .main, .panel, .panel.bright .rule,
      .panel.center, .panel.tight, .panel.wide, .rule, .status, .strip,
      .strip .hint, .subtitle, .title, html, body

    template      HEAD restates                      now
    picker.html   8  (* .band .frame .head .main .strip .strip .hint html, body)   0
    options.html  11 (* .foot .frame .head .lead .main .rule .strip .strip .hint .title html, body) 0
    chat.html     9  (* .eyebrow .foot .frame .head .rule .status .title html, body) 0
    layout.html   14 (* .body .eyebrow .foot .frame .head .lead .main .rule .status .strip .strip .hint .title html, body) 0

    shorthand trap: findall on `.notice{margin: 140px var(--inset) 0 var(--inset);}` -> ['margin'] (fails)
                    findall on the longhand form -> [] (passes)

So `TestChromeComposition.test_no_template_restates_a_chrome_rule` is red on
the pre-change tree (42 restatements across the four surfaces) and green now,
and `test_no_shorthand_carries_a_var` catches the trap that cost this
increment a debugging round (the intermediate chrome used the shorthand
form and every band silently lost its inset).

## Note on the stop condition

The command above exits zero, but that is a **floor, not the finish line**:
the gate is a ratchet with 24 exemptions, most views still hand-draw, and the
component vocabulary has only two of its five components (`system_buttons`
and `ui.base.chain`). The layout presets and the capability vocabulary landed
in this increment; what is left is the components themselves (`shell`,
`tile`, `panel`, `stat`) and the migration of the views that need them. The
stop condition became reachable because the gate exists and the health gate
stays green while the migration is in flight — which is exactly what it was
designed to allow.
