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
- **The view accents** are tokens too: `ACCENT_SLOTS` (one accent per view
  name) is resolved by `renderer_registry` into every renderer entry and
  read by `playlist_color.accent_for`, so no shipped renderer module holds
  an `ACCENT` literal any more (section 2e).

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
- `renderers/ui/tile.py` — the **tile**: a bounded box with a label, built in
  this increment. One definition of the declared-box rule (`content_size`: a
  declared width is the CONTENT box, so a tile declares its rect minus the
  frame), the label fit (`fit_size`) and the centring (`label_box`), plus
  both a markup half (`cell`/`layer`, what the picker and options fill their
  raw slot with) and a drawing half (`draw`, used by the touch-confidence
  region map). Its look is authored once in the shared stylesheet
  (`_chrome.html`): `.tile`/`.lab`/`.shade` for a filled tile,
  `.cell`/`.name` for a plain one, every colour a token (`--on-accent`,
  `--label-shade`). See §2b.
- `renderers/ui/grid.py` — a **grid of tiles**: `columns` (how many columns
a region takes — read off its shape when the caller does not state one)
and `grid` (the row-major rects, the gutter, the box count bound). The
picker and the options name grid both ask it, so the two can no longer
disagree about where a box goes; a narrow 288px band is ONE application
column. See §2f.
- `renderers/ui/text.py` — one font resolution, one measurement, one
  fitting rule (`font`/`face`/`width`/`fit`/`write`/`line`). `_font`,
  `_font_or_default`, `_fit` and a truncation loop used to exist in five
  views.
- `renderers/ui/shell.py` — the band a full-panel view wears: `head`
  (title, detail, health dot, its honest age, the rule under it), `foot`,
  `rule`, `health_ink` (the poll-health vocabulary → palette role) and
  `age`.
- `renderers/ui/stat.py` — a label plus a value line: `label`/`value`/
  `body`, `row` (the component itself), `meter` (a clamped fraction bar)
  and `width`. See §2c.

### 2b. The tile component (this increment)

`_picker_tiles.py` and `_options_grid.py` each carried the same four
helpers — `content_size` (border compensation), `label_px`
(shrink-until-it-fits), `label_box` (measured centring) and `hex_colour` —
plus their own cell builder. They were the same component twice, because
nothing owned “a tile”. They now call `renderers/ui/tile.py`:

| owner | what it owns now |
| --- | --- |
| `ui/tile.py` | the box rule, the fit rule, the centring, the markup half (`cell`/`layer`), the drawing half (`draw`) |
| `_chrome.html` (the `css` section) | the look: `.layer`, `.tile`/`.lab`/`.shade`, `.cell`/`.name`, all `var(--…)` |
| `_picker_tiles.py` | the picker's chrome variables + its title |
| `_options_grid.py` | the name-count policy (`cell_count`/`cols_for`) + the chrome variables; the arithmetic itself is `ui/grid.py` (§2f) |

The look moved into the one shared stylesheet, which also deleted the
`.layer` rule that picker, options and chat had each restated (3 copies →
1). Two palette roles were added for it: `on-accent` (`#120c20`, the ink and
frame on a bright fill — it was `picker.INK` and
`retro_grid_draw.DEFAULT_INK` as well) and `label-shade` (`#5a4670`, the
offset copy behind a label). `picker.html` and `options.html` have no CSS of
their own left at all, and `picker.html` is now in
test_theme's no-literal list, so a colour literal in a panel template fails
the suite.

What the tests pin:

- **the cross-language constant**: `_chrome.html`'s `border-width` must
equal `ui.tile.BORDER` (`tests/test_tile.py`), so the drawn tile stays the
same size as the region that taps it;
- **the anti-drift rule**: no panel template may restate `.tile`/`.cell`/
`.layer` (same test), and the shared stylesheet's tile rules carry tokens
only;
- **one fit rule**: `fit_size` / `label_box` are the only implementations
(`tests/test_picker.py` no longer reaches a private picker copy);
- **the trust boundary**: a hostile label is escaped inside `cell`, and a
hostile *colour* string is refused by `css_colour` (hex only) rather than
reaching a style attribute;
- **the palette really resolves**: `test_theme` renders a tile-styled document
  and asserts the frame pixel is `on-accent` and its fill `label-shade`, plus
  a picker render whose tile frame is `on-accent`.

Both halves keep the layer's obligation: `draw` returns the frame unchanged
on any failure, and `cell`/`layer`/`content_size` are total on garbage.

### 2c. The shell, stat and text components (this increment)

Three components, one deletion each.

`services_draw` and `resources_draw` were the same forty lines twice. Each
carried its own `_font`/`_font_or_default`/`_fit`/`_age`, its own eight
colour constants (`C_BG`, `C_TEXT`, `C_DIM`, `C_LINE`, `C_OK`, `C_WARN`,
`C_BAD`, `C_UP`, `C_DOWN`, `C_FAILED` — sixteen tuples across the pair),
and a header band that differed only in variable names. A third copy of the
band sits in `row_draw`, and `stream`/`text` carry `_font`/`_fit` again.

That duplication is gone:

| component | what it owns |
| --- | --- |
| `ui/text.py` | the face at a size, the width of a string, the trim-to-room rule, one line of type and a hairline. `face` is never None (a host with no font package gets Pillow's bitmap face) and nothing raises. |
| `ui/shell.py` | `PAD`/`HEAD_SIZE`/`RULE_Y`/`FOOT_SIZE`, `head` (title + detail, the health dot and its status line, the rule), `foot`, `rule`, `health_ink` (one map for the poll-health vocabulary `cold`/`warm`/`stale`/`error`) and `age`. |
| `ui/stat.py` | the type scale (`44/170/130/40/36/30`), `label`/`value`/`body`, `row` (a label with its value under it — the component), `meter` (outline `edge`, fill the caller's ink, clamped so 300% cannot paint outside its rect) and `width`. |

`resources_draw` went 209 → 159 lines and `services_draw` 229 → 189, with
no colour, no font loader, no age line and no truncation rule left in
either; both are now pure "which number goes where" over the layer. The
gate's exemption list went **24 → 22**.

The health map is the quiet win: the four health words are declared by
`resources_poll`, `services_poll`, `row_poll` and `beads_poll`, and three
views each held their own copy of the mapping to four colours. It is one
table now (`shell.HEALTH_ROLE`), and an unknown word is a grey dot rather
than a `KeyError`.

What the tests pin (`tests/test_shell.py`, 26 tests):

- the band's rule is the palette's **`rule` role** and the dot wears the
  **health role** (`ok`/`attention`/`alert`/`muted`), on rendered pixels —
  the pre-change colour was `C_LINE = (60, 60, 70)`;
- `face` never returns None and every entry point returns the frame
  unchanged on failure, including `meter` on a garbage rect;
- the meter is clamped and empty-but-outlined when the fraction is unknown
  (`0`, `None`, `"junk"`), so an unreadable source is an empty bar, not a
  bar that spills;
- **the one cross-path pin that holds today**: the rule under the band is
  `--rule` in the shared stylesheet *and* `theme.rgb("rule")` in the Pillow
  band, so the two paths cannot drift on the one thing both draw;
- the two migrated views hold no `PIL` import, no hex literal and none of
  the deleted helpers.

### Which views migrated, which are left

Migrated so far, in order: the persistent overlay chrome (both system
buttons); the panel's two tile layers (the picker grid, the options name
grid), now one component with two halves; the two polled list views
`resources_draw` and `services_draw`, now composers over `ui.shell` +
`ui.stat` (`ui.text` underneath both); and this increment's three card
views, `notice`, `text` and `sleep`, now composers over `ui.panel`.

Deliberately left (still Pillow, still drawing by hand): the other 19
modules in the gate's exemption list — `beads` with its `beads_detail`/
`beads_detail_card`/`services`-style draw helpers, `row_draw`, `macbook_draw`/
`macbook_strip`, `qr`/`qr_common`, `reload`, `stream`, `activity`, `clock`,
`retro_grid_draw`, `touch_confidence_draw`, `feed_health`, `life`,
`playlist`/`playlist_bar` (the daemon-side progress bar), and
`_html_error`/`_html_native`. Reason: the vocabulary they need is now built
(`tile`, `text`, `shell`, `stat`, `panel`) but each of them is a real
migration — `beads` and `row_draw` are several surfaces each, `reload` owns
the QR proof and its scan relay, `stream` is a live frame at a capped fps,
and `_html_native` is the engine shim rather than a view.
`touch_confidence_draw` is partly migrated (its region boxes are
`ui.tile.draw` now) but keeps its own accent bar, title and diagnostics
lines, so it stays on the list until `shell`/`panel` are applied to it — a
half-migrated file must not claim to have left the ratchet.

### 2d. The panel component, and the three card views it pulled (this increment)

`notice`, `text` and `sleep` were the same card with different words:

- `notice` drew a severity bar across the top, a headline centred above a
  body and a severity tag in the bottom corner, with `_font`,
  `_shrink_to_fit` (a binary search over the *first line's* width) and a
  three-entry RGB severity map;
- `text` drew one auto-fitted message centred on the panel, with its own
  `_font`/`_fits`/`_autofit` (a binary search over the *whole block's* box);
- `sleep` drew a centred hint with a third `_font`.

Those two searches are two halves of one rule, and that rule is now
`ui/panel.py`:

| what | owner |
| --- | --- |
| "these words fit this region" | `panel.fit_size` / `panel.fits` — the largest size up to the declared one whose whole block fits, `MARGIN` applied inside (the caller states the region, not the room) |
| the title | `panel.headline` — scaled to the region, then centred, `ink-strong` by default |
| the body | `panel.block` — a centred multi-line block, hung from a top edge when something sits above it |
| the corner label | `panel.tag` — bottom-left inset by `PAD`, `muted` by default |
| the accent bar | `panel.bar` — the full-width band across the top, the palette accent by default |
| the component | `panel.card` — bar, title, body, tag; the title is the only required part |

The notice's severity map is gone, not aliased: `SEVERITY_ROLE` maps
`info`/`warn`/`critical` onto the palette roles `accent`/`attention`/`alert`,
so the fourth copy of "red" and the third copy of "amber" are whatever
`theme.py` says. **That is a deliberate pixel change**: the critical bar went
from the notice-local `(255, 70, 70)` to `theme.rgb("alert")`
`(214, 74, 74)`, and `tests/test_policy.py`'s severity-bar assertion now
reads the token (same intent: "the critical notice wears the critical
colour"). The three views' backgrounds/inks default to `page`, `ink-strong`
and `faint` instead of `(0, 0, 0)`/`(10, 10, 14)`/`(70, 74, 88)`, and the
`text`/`notice` param help text says so.

The `text` view's no-font fallback also changed for the better: it used to
throw the message at `(20, 20)` in Pillow's bitmap face; the component's
`ui_text.face` is never None, so a host with no font package now gets the
same centred block in the fallback face. One fit rule is also stricter than
the old `text` behaviour: an explicit `size` is now a *ceiling* (a block that
still overflows is scaled down rather than clipped off the panel).

What `tests/test_panel.py` (21 tests) pins:

- the returned size is the LARGEST that fits (`fits(size)` true,
  `fits(size + 1)` false), the whole block is scaled and never cut, and the
  margin is the component's own (`MARGIN` applied inside);
- the fit search is total: no room or an unmeasurable screen keeps the
  declared size, garbage gives `FLOOR`, `None`/`""`/`object()` never raise;
- the bar, headline, body and tag wear the palette roles on rendered pixels,
  a caller's ink replaces the role, and the tag sits at the region's own
  corner (`PAD` inset) rather than wherever a view used to put it;
- `card` returns the frame unchanged on a broken screen and returns `None`
  for a `None` frame (the layer's never-blank obligation);
- the three views hold no `from PIL`, no `ImageDraw`/`ImageFont`, no
  `_font`/`_fits`/`_autofit`/`_shrink_to_fit`, no hex literal **and no
  `ACCENT` at all** (the assertion was tightened this increment: their
  identity colour is the palette slot, not a literal they carry);
- `notice.severity_ink` is a role lookup for every severity and is total on
  an unknown word, and the rendered bar is exactly `theme.rgb("alert")` /
  `attention` / `accent` for critical / warn / info;
- `text` is centred (ink on both sides of the middle) on the `page` role,
  honours an explicit `size` and `background`, and draws nothing when there
  is no message; `sleep` names the way back in the `faint` role.

The gate's exemption list went **22 → 19** (`renderers/notice.py`,
`renderers/text.py`, `renderers/sleep.py` removed): the gate now prints
`19 shipped module(s) still draw by hand`.

### 2e. The accent token — the last per-view colour constants (this increment)

`ACCENT = "#rrggbb"` lived in **sixteen** renderer modules and
`playlist_color.accent_for` read it off the module by name, while
`theme.ACCENT_SLOTS` already held the same table. That is defect 2 in
miniature: one colour, two owners, and the palette one was dead.

Now the palette is the only owner:

| step | what owns it |
| --- | --- |
| the value | `theme.ACCENT_SLOTS[view]` (one entry per view identity) |
| the declaration | `renderer_registry.load_renderers` resolves the slot into each entry it loads as `accent_slot` (a view the palette does not know gets `None`) |
| the resolution | `playlist_color.accent_for`: per-view item `color` > a renderer's **own** `ACCENT` (a plugin's opt-in, still honoured) > the entry's `accent_slot` > the playlist default |
| the drawing | a view that paints with its accent calls `theme.accent_rgb(NAME)` / `theme.rgb("dock")`, never a local copy |

Sixteen literals deleted (`activity`, `chat` (a tuple), `clock`, `macbook`,
`macbook_glance_color`, `notice`, `options`, `picker`, `reload`,
`retro_grid`, `row`, `stream`, `text`, `touch_confidence`, `unified`,
`unified_dock`). `macbook_glance_color` was a values-only colour module and
is now values-only without the accent; `unified_dock`'s stale amber
`(255, 180, 80)` was the palette's `attention` to the byte, so it became
`theme.rgb("attention")` while its own grey step `DIM` stayed (the palette
has no role that equals it). Every slot value is byte-identical to the
literal it replaced, so **this is a pure ownership change: not one panel
pixel moves** — which is why the evidence below checks equality rather than
a diff.

What pins it (`tests/test_theme.py::AccentOwnershipTest`, 4 tests, plus the
tightened `tests/test_panel.py` assertions):

- no shipped renderer module declares `^ACCENT =` (offenders named);
- every loaded entry's `accent_slot` equals `theme.accent(name, None)`, and
  for a slotted view `accent_for(entry)` equals `theme.accent_rgb(name)`
  while an un-slotted view still falls through to the playlist default;
- a hand-built entry with a module `ACCENT` still beats the slot (the
  plugin opt-in);
- the four modules that *draw* with an accent (`reload`,
  `touch_confidence`, `unified_dock`, `macbook_glance_map`) reference
  `theme.` and name no `ACCENT`;
- `tests/test_panel.py` now fails on **any** hex literal or `ACCENT` in
  `notice`/`text`/`sleep`, and `tests/test_unified.py` /
  `tests/test_macbook_preview.py` read `theme.rgb("dock")` /
  `theme.rgb("macbook")` instead of `dock.ACCENT` / `glance.ACCENT`.

### 2f. The grid of tiles, and the 15-70-15 application column (this increment)

`tile` owned one box; nothing owned a grid of them, so the arithmetic
lived **twice**: `picker.grid_geometry` and `_options_grid.grid_geometry`
carried the same row count, the same `cw`/`ch` division, the same gutter
rule and the same row-major walk. The duplication was quiet because the
two differed in ways that looked like policy — the picker capped at three
columns, options at two, options bounded the name count and the picker did
not — and meanwhile the picker's `cols` parameter was **dead**: it was
overwritten by `cols = max(1, min(DEFAULT_COLS, count))` before it was
used, so a caller could not state a shape at all.

Now `renderers/ui/grid.py` is that one definition, and a surface keeps
only its own policy as data:

| what | owner |
| --- | --- |
| how many columns a region takes | `ui.grid.columns` — an explicit `cols` (clamped to the surface's cap and to the count) wins; absent one, the count that makes a box closest to square, read off the region's shape |
| where each box lands | `ui.grid.grid` — row-major, the declared gutter or the shape-derived one, never a box under one pixel |
| how many boxes | `ui.grid.TILE_CAP = 48`, applied inside the component, because a public geometry function handed `10 ** 9` allocated a rect per box (an out-of-memory, not a validation error) |
| the picker's cap (3) | `picker.DEFAULT_COLS`, still the picker's |
| the options count rule | `_options_grid.cell_count` / `cols_for`, and `MAX_CELLS` is now `ui.grid.TILE_CAP` |

The shape-derived count is what the objective's `15-70-15` case needed.
A 288px band through the old fixed rule:

    HEAD  picker.grid_geometry([24, 40, 240, 860], 8)      -> (32, 48, 69, 276)   three 69px columns
    HEAD  picker.grid_geometry([24, 40, 240, 860], 8, 1)   -> (32, 48, 69, 276)   the `cols` argument was IGNORED
    now   ui.grid.grid(band, 8, ui.grid.columns(band, 8, cap=3)) -> (32, 48, 224, 98)  ONE application column

and the full panel is untouched at three columns for every view count
(`grid_geometry(default_rect(1920,1080), 8)` is `(179, 59, 508, 261)`
before and after), as is the merged home grid (`unified.default_grid`,
same three columns for 8–24 views). One visible change for a custom rect:
a THREE-view picker now draws two columns instead of three (a
771x401 pair and one below, rather than three 508x822 slivers) — that is
the same rule being honest about the shape, and nothing pinned it.

`cols` is now a real param (`picker.PARAMS`), read by `coerce_cols`,
honoured by `grid_geometry`, `picker_regions` and therefore by the CLI
(`--cols`), and — the part that keeps the invariant true — read by
`touch_audit_regions.expected_for_view`, so the audit recomputes the same
rects the renderer drew when a caller states a shape. Garbage falls back
to the shape (`"wide"`, `0`, `True`, `None` all mean "derive it").

A label is why the shape rule reads the way it does: a box is filled with
text across its width, so a box taller than it is wide is the wrong shape
for one. At the band's 224px the label fit is 20px; at the old 69px it was
the 12px floor.

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
→ 0/0/0/0. After the tile component landed (`2b`) the per-template residue
is smaller still: picker 25→0, options 24→0 (their whole stylesheet is the
include), chat 26→10, and the tile's own look (8 rules) lives in the same
`css` section, so all four surfaces are styled from one file.

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
- `picker.html`’s tile layer is no longer a reason to leave it out: the two
  literals became the tile component’s palette roles (`on-accent`,
  `label-shade`), so `picker.html` is in the “no colour literal” list and
  the tile rules are shared (see `2b`).

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

19 exemptions remain (the list is in the tool, sorted).

## Adding a new view from the layer

1. Give the view a module under `renderers/` as today — `run(screen,
   params, stop)`, plus `NAME`, `DESCRIPTION`, `PARAMS`, `INPUTS`,
   `STATIC`, and `CAPABILITY` when it can render into a region smaller
   than the panel (`partial`, or `primary` if it may be a preset's centre;
   omitted means `full`, and `POST /layout` will refuse it in a band).
2. Take every colour from `renderers/theme.py`, including the view's own
   identity accent: do **not** declare `ACCENT` — add the view to
   `theme.ACCENT_SLOTS` and the registry resolves it into the renderer
   entry, so the playlist bar, `GET /renderers` and the picker all agree.
   A `#rrggbb` in a view is a defect; in a migrated template
   `tests/test_theme.py` fails on one, and no shipped module may declare
   `ACCENT` (`tests/test_theme.py::AccentOwnershipTest`).
3. Draw through the component layer:
   - a template view renders a named template from `html-templates/`. The
     token block reaches it automatically. If it draws panel chrome, it
     includes the shared sections (`<!--#include css-->` and the band it
     wants) and names a variant on its `<body>`: the chrome is authored once
     in `_chrome.html`, and `tests/test_html.py` fails if the template
     restates one of its rules or invents a variant;
   - a Pillow view needs a component in `renderers/ui/` — and if the
     component it needs does not exist, that is the next component to
     write, not a reason to draw by hand. A labelled box is `ui.tile.draw`;
     a grid of them is `ui.tile.layer` (markup) with the look from the
     shared stylesheet, and where the boxes go is `ui.grid` (the column
     count and the rects); the band a full-panel view wears is `ui.shell`
     (`head`/`foot`/`rule` + the health dot); a label with its value is
     `ui.stat.row`, a fraction is `ui.stat.meter`; centred words are
     `ui.text.write`/`ui.panel.card` (a title, an optional body, a corner
     tag, an accent bar, and the one scale-to-fit rule).
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
advertised view — and that list draws as ONE application column, because
the grid reads the band's own shape (section 2f) instead of a fixed
three columns.

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

- ~~**Per-view colour constants**~~ — **done this increment** (section
  2e): no shipped renderer declares `ACCENT`, `theme.ACCENT_SLOTS` is the
  only owner, and `accent_for` resolves the slot through the renderer
  entry. A plugin from outside this tree may still declare its own
  `ACCENT`, which is honoured ahead of the slot.
- **The card vocabulary could still pull more views**: `feed_health`'s
  cold-start/error cards and `beads_detail_card` are a title plus a body,
  and `touch_confidence_draw`'s bar/title/diagnostics is the other obvious
  pull (`panel` + `stat` now cover it). Left because each is a bigger
  surface than the three card views and needs its own test story.
- **The band still has two definitions, one per rendering path**: the shared
  stylesheet's `.frame`/`.head`/`.title`/`.rule`/`.foot` rules for
  templates, and `ui/shell.py`'s constants for the Pillow views. That is one
  definition *per path* rather than one definition overall, and the numbers
  genuinely differ (Pillow `PAD = 60` vs the chrome's `--inset: 64px`), so
  only the rule colour is pinned across the paths (`--rule` ↔
  `theme.rgb("rule")`, `tests/test_shell.py`). Unifying the geometry needs a
  cross-language constant, the way `ui.tile.BORDER` ↔ the stylesheet's
  `border-width` already works in `tests/test_tile.py`.
- **The control page does not offer the styles yet.** `GET /layout/presets`
  publishes them and `POST /layout {"preset": ...}` applies them, but the
  phone page has no style picker and no per-slot view selects. That is the
  operator surface for the “offered per style” rule and the next UI step.
  (The per-view colours are no longer part of that surface's work: a
  renderer list now carries the palette accent, not a view literal.)
- ~~**A narrow-band application column.**~~ — **done this increment**
  (section 2f): the column count is a component decision read off the
  region's shape, so a 288px band is ONE column of 224px tiles (label fit
  20px) instead of three 69px ones (12px floor), `cols` is a real param,
  and `picker_regions` generates the matching tap rects. A dedicated
  narrow-band renderer is no longer needed.
- **The remaining hand-drawing views** (the gate's 19 exemptions) are the
  bigger migration: `beads*`, `row_draw`, `macbook_draw`/`macbook_strip`,
  `qr`/`qr_common`, `reload`, `stream`, `activity`, `clock`,
  `retro_grid_draw`, `touch_confidence_draw`, `feed_health`, `life`,
  `playlist`/`playlist_bar` and the two `_html_*` non-views. The vocabulary
  they need all exists now (`tile` + `grid`, `text`, `shell`, `stat`,
  `panel`), so
  each is a straight migration with its own test story, not new design.
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
    component layer ok: 19 shipped module(s) still draw by hand; all exempt, none stale
    Ran 331 tests in 52.5s
    OK
    line budget ok: all source files within 250 lines
    $ echo $?
    0

    Also run this increment (not part of the gate, all affected):
    tests.test_grid      Ran 17 tests, OK      (new: the grid component)
    tests.test_picker    Ran 34 tests, OK      (was 29; +5 ApplicationBandTest)
    tests.test_options   Ran 26 tests, OK
    tests.test_unified   Ran 29 tests, OK
    tests.test_tile      Ran 23 tests, OK
    tests.test_theme     Ran 27 tests, OK
    tests.test_touch_audit / test_touch_resolve / test_layout_presets / test_components
                         Ran 119 tests, OK
    tests.test_touch / test_home_chrome / test_touch_confidence / test_mcp /
        test_repo_health / test_deploy_html_step
                         Ran 257 tests, OK

    $ python3 -m unittest tests.test_row tests.test_stream tests.test_sleep \
        tests.test_qr tests.test_reload tests.test_beads \
        tests.test_resources_services tests.test_retro_grid tests.test_panel \
        tests.test_tile tests.test_grid tests.test_playlist \
        tests.test_policy tests.test_feed tests.test_feed_health \
        tests.test_feedback
    Ran 438 tests in 130.5s -> OK (skipped=10)

    $ python3 tools/check-repo-health.py
    line budget ok: all source files within 250 lines
    ok: no generated native artifacts tracked
    component layer ok: 19 shipped module(s) still draw by hand; all exempt, none stale
    rc=0

    Full `discover` this increment (the three known-red modules on this box
    are never run as a gate: tests.test_mac_zoom,
    tests.test_deploy_reload_proof, tests.test_talon_apps):

    $ python3 -m unittest discover -s tests
    Ran 1440 tests in 354.909s
    FAILED (failures=2, errors=1, skipped=10)
    -> exactly the three known-red modules, nothing else

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

### The panel component, the buttons and every layout style — re-rendered this increment

`DISPLAYD_FAKE_FB=1 python3 /tmp/panel_evidence.py` — real daemon on a
loopback ephemeral port, temp policy/feedback paths, real HTTP:

    == daemon ==
    display: {'width': 1920, 'height': 1080, 'bpp': 32, 'stride': 7680, ...}  version: 0.8.0

    == layout styles (system buttons over every style) ==
      full                   style=full       first_pixel_ms=5.1 badges=(True, True)
          regions: view=(0, 0, 1920, 1080)
      split-50-50            style=split-50-50 first_pixel_ms=3.9 badges=(True, True)
          regions: top=(0, 0, 1920, 540), bottom=(0, 540, 1920, 540)
      split-50-50-columns    style=split-50-50-columns first_pixel_ms=3.8 badges=(True, True)
          regions: left=(0, 0, 960, 1080), right=(960, 0, 960, 1080)
      15-70-15               style=15-70-15   first_pixel_ms=4.6 badges=(True, True)
          regions: left=(0, 0, 288, 1080), center=(288, 0, 1344, 1080), right=(1632, 0, 288, 1080)

    == system buttons over a template view (html/status) ==
      home+sleep badge tiles on the frame: (True, True)
      error card on the frame: False (False = a real template render)
      template accent px: 1059  panel px: 29406  badge px: 29416
      first_pixel_ms=5.6

    == migrated card views ==
      notice critical switch=notice first_pixel_ms=5.6 bar(960,9)=(214, 74, 74) is_old_literal=False badges=(False, False)
          most common colours: [((7, 8, 12), 2014075), ((214, 74, 74), 35393), ((255, 255, 255), 14140)]
      notice warn     switch=notice first_pixel_ms=0.9 bar(960,9)=(255, 180, 80) is_old_literal=False badges=(False, False)
      text            switch=text   first_pixel_ms=5.5 bar(960,9)=(7, 8, 12) is_old_literal=False badges=(True, True)
      sleep           switch=sleep  first_pixel_ms=5.4 bar(960,9)=(7, 8, 12) is_old_literal=False badges=(False, False)

    == notice severity is the palette role, not a fourth red ==
      theme.rgb('alert')=(214, 74, 74)  old notice literal=(255, 70, 70)
      theme.rgb('accent')=(127, 209, 255)  theme.rgb('attention')=(255, 180, 80)

Reading it: every style still paints its first pixel in 3-10ms (budget
100ms) with both badges present; the html template view really rendered
(`error card on the frame: False`, accent 1059px, panel 29406px) with the
badges over it; the critical notice bar is `(214, 74, 74)` — the alert
ROLE, and provably NOT the old view-local `(255, 70, 70)`; the warn bar is
`attention`; `text` and `sleep` sit on the `page` role `(7, 8, 12)` with
the badges suppressed on `sleep` and shown on `text`; and the frame still
reports 1920x1080 with page as its dominant colour. Frames saved:
`/tmp/panel-evi-{full,split-50-50,split-50-50-columns,15-70-15}.png`,
`/tmp/panel-evi-html-buttons.png`, `/tmp/panel-evi-notice-critical.png`,
`/tmp/panel-evi-notice-warn.png`, `/tmp/panel-evi-text.png`,
`/tmp/panel-evi-sleep.png`.

### The panel assertions fail before, pass after

No execution needed: the three views themselves were the pre-change state,
and `git show HEAD:<file>` proves what the new assertions are red on.

    $ git show HEAD:renderers/notice.py | grep -n 'ImageDraw\|def _font\|def _shrink_to_fit\|(255, 70, 70)\|\(10, 10, 14\)'
    11:from PIL import ImageDraw, ImageFont
    29:    "info": (90, 200, 255),
    30:    "warn": (255, 165, 0),
    31:    "critical": (255, 70, 70),
    38:def _font(screen, name, size):
    48:def _shrink_to_fit(draw, text, font, max_width):
    83:    bg = screen.color(params.get("background"), (10, 10, 14))
    86:    draw = ImageDraw.Draw(img)

    $ git show HEAD:renderers/text.py | grep -n 'ImageDraw\|def _autofit\|(0, 0, 0)'
    3:from PIL import ImageDraw, ImageFont
    25:def _autofit(draw, text, path, width, height, margin=0.88):
    44:    bg = screen.color(params.get("background"), (0, 0, 0))

    $ git show HEAD:renderers/sleep.py | grep -n 'ImageDraw\|def _font\|(70, 74, 88)'
    14:from PIL import ImageDraw, ImageFont
    33:def _font(screen, size):
    50:    fg = screen.color(params.get("color"), (70, 74, 88))

    $ python3 -c "...; print((255, 70, 70) == theme.rgb('alert'), theme.rgb('alert'))"
    False (214, 74, 74)

So on the pre-change tree: `tests/test_panel.py`'s structural test (no
`from PIL`, no `ImageDraw`/`ImageFont`, no `_font`/`_fits`/`_autofit`)
fails on all three files; the severity-role assertion fails because the old
literal is not `theme.rgb("alert")`; and the tag/centring/background
assertions fail because those files had no component to read `page`,
`ink-strong`, `muted-soft` or `faint`. The gate also fails on the old tree
with "3 stale exemption(s)" once the three entries are removed from
`EXEMPTIONS` — which is exactly the two-directional ratchet working.

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

### The tile component, both paths (this increment)

    $ python3 tools/check-components.py
    component layer ok: 24 shipped module(s) still draw by hand; all exempt, none stale

    $ python3 -m unittest tests.test_tile -v          -> Ran 23 tests, OK
    $ python3 -m unittest tests.test_picker tests.test_options tests.test_theme \
        tests.test_components tests.test_html tests.test_unified tests.test_chat
      -> OK (96 + 182 tests respectively across the two sweeps)
    $ python3 -m unittest tests.test_touch_confidence tests.test_retro_grid
      -> Ran 57 tests, OK

    $ python3 tools/check-repo-health.py               -> rc=0
    (line budget, no tracked generated artifacts, component layer)

`python3 /tmp/tile_evidence.py` — real daemon on a loopback ephemeral port,
`DISPLAYD_FAKE_FB=1`, temp policy/feedback/deploy paths, 1920x1080:

    daemon: DISPLAYD_FAKE_FB=1 http://127.0.0.1:63566

    POST /show picker -> snapshot (1920, 1080)          # the MARKUP half
      6 tiles, first rect=(179, 59, 508, 401)
      tile frame pixel == on-accent  : True (18, 12, 32)
      tile fill pixel  == palette[0] : True (90, 200, 255)
      gutter pixel     == the bg param: True (8, 10, 16)

    POST /show touch_confidence -> snapshot (1920, 1080) # the DRAWING half
      region outlines from ui.tile.draw: [(80, 220, 120), (90, 200, 255)]

    POST /layout preset=full -> 200, regions=[('view', …)]
      colours=4199  on-accent px=227213  first_pixel_ms=2.4  badges=True
    POST /layout preset=split-50-50 -> 200
      colours=4231  on-accent px=144415  first_pixel_ms=2.3  badges=True
    POST /layout preset=split-50-50-columns -> 200
      colours=4898  on-accent px=130143  first_pixel_ms=4.9  badges=True
    POST /layout preset=15-70-15 -> 200
      colours=2655  on-accent px=25785   first_pixel_ms=3.4  badges=True

    ui.tile.BORDER = 6  content_size((0,0,300,150)) = (288, 138)

So the tile's frame is the palette's `on-accent` token on the template path,
the region map is drawn by the same component on the Pillow path, and the
tile frame is still present when the picker is put in a 288px band (25,785
`on-accent` pixels in `15-70-15`) — with both system buttons and every style
painting its first pixel in 2-5ms (budget 100ms). Frames saved at
`/tmp/tile-evidence-picker-1920x1080.png`,
`/tmp/tile-evidence-touch-confidence-1920x1080.png` and
`/tmp/tile-evidence-<style>-1920x1080.png`.

### The tile rules fail before, pass after (this increment)

Deterministic pre-change probes (`git show HEAD:<file>`), against the four
new assertions:

    $ git show HEAD:html-templates/picker.html | grep -n '#120c20\|#5a4670\|\.layer'
    55:  .layer { … }
    56:  .tile { position: absolute; border: 6px solid #120c20; }
    57:  .tile .lab { … color: #120c20; }
    58:  .tile .shade { … color: #5a4670; }

    $ git show HEAD:html-templates/options.html | grep -n '\.layer\|\.cell\|\.name'
    64:  .layer { … }
    65:  .cell { position: absolute; }
    66:  .cell .name { position: absolute; font-weight: bold; }

    $ git show HEAD:html-templates/chat.html | grep -n '\.layer'
    69:  .layer { … }                      # the same rule, a third copy

    $ git show HEAD:html-templates/_chrome.html | grep -c border-width
    0                                    # the tile look was not shared at all

    $ git show HEAD:renderers/_picker_tiles.py | grep -n '^def content_size\|^def label_px\|^def label_box'
    40:def content_size(rect):
    47:def label_px(name, cw, start=…):
    72:def label_box(name, rect, size):   # all three also existed in _options_grid.py

So, with the newly added assertions evaluated against those pre-change
sources (that is what "fails before" means for a structural test):
`test_no_colour_literal_in_a_migrated_style` (picker had two literals),
`test_the_tile_rules_live_in_the_one_shared_stylesheet` and
`test_the_stylesheet_frame_is_the_component_border` (no tile rule in the shared
sheet), `test_no_surface_restates_the_tile_look` (four restatements above) and
`test_the_tile_tokens_resolve_on_the_panel` (no such token or rule) all fail on
the old sources and pass now.

### The shell, stat and text components, and the two views they pulled (this increment)

    $ python3 tools/check-components.py
    component layer ok: 22 shipped module(s) still draw by hand; all exempt, none stale

    $ python3 -m unittest tests.test_shell -v        -> Ran 26 tests, OK
    $ python3 -m unittest tests.test_shell tests.test_components \
        tests.test_resources_services tests.test_theme tests.test_layout_presets
      -> Ran 118 tests, OK (skipped=2)

    $ python3 tools/check-repo-health.py            -> rc=0
    (line budget, no tracked generated artifacts, component layer)

`python3 /tmp/shell_evidence.py` — a real `DisplayDaemon` on the in-memory
framebuffer (`DISPLAYD_FAKE_FB=1`), temp policy/feedback paths, 1920x1080,
with a populated snapshot injected into each poll store and the view
rendered through `daemon.show()` then `/state` and `/snapshot`:

    resources  size=(1920, 1080) first_pixel_ms=11.0
               tokens: page=1916041, rule=3230, edge=11588, ink=42114,
                       muted=4856, ok=20419, alert=15741
    services   size=(1920, 1080) first_pixel_ms=17.5
               tokens: page=1964452, rule=3230, edge=7204, ink=9378,
                       muted=10908, ok=5822, attention=2302, alert=5694
    resources-cold size=(1920, 1080) first_pixel_ms=1.3   rule=3602
    services-error size=(1920, 1080) first_pixel_ms=0.8   rule=3602

    recorded:
      resources      -> /tmp/shell-evidence-resources-1920x1080.png
      services       -> /tmp/shell-evidence-services-1920x1080.png
      resources-cold -> /tmp/shell-evidence-resources-cold-1920x1080.png
      services-error -> /tmp/shell-evidence-services-error-1920x1080.png

Every frame carries the band's `rule` token and the `edge` token (the row
rules and the meter outlines); `ok`/`attention`/`alert` appear exactly where
the state calls for them (CPU colour, memory meter, the failed-unit row, the
stale status dot). The `rule` count is below the full 2x1800 on the populated
frames because the always-on home and sleep badges cover the band's two ends
— expected, and itself the component layer working. First pixel 0.8–17.5 ms
against the 100 ms budget (the 17.5 ms is a cold first frame of a real view;
steady ticks are 1–5 ms).

### The new assertions fail before, pass after (this increment)

    $ git show HEAD:renderers/resources_draw.py | grep -n '^C_LINE\|^C_OK\|ImageDraw.Draw\|def _fit\|def _age\|def _font_or_default\|def _bar'
    20:C_DIM = (140, 140, 150)
    21:C_LINE = (60, 60, 70)
    22:C_OK = (80, 220, 120)
    47:def _font_or_default(screen, name, size):
    51:def _fit(draw, text, font, max_w, max_chars=90):
    83:def _age(updated):
    96:def _bar(draw, x, y, w, h, frac, color):
    106:    draw = ImageDraw.Draw(img)

    $ git show HEAD:renderers/services_draw.py | grep -c 'ImageDraw\|^C_'
    10
    $ git show HEAD:renderers/resources_draw.py | grep -c theme   -> 0
    $ git show HEAD:renderers/services_draw.py  | grep -c theme   -> 0
    $ python3 -c "import theme; print(theme.rgb('rule'), theme.rgb('edge'))"
    (36, 64, 92) (43, 50, 64)

So, evaluated against those pre-change sources:
`test_neither_view_draws_or_owns_a_palette_of_its_own` (both modules import
`PIL.ImageDraw`, define `_fit`/`_age`/`_font_or_default`, and hold `C_*`
colour tuples) and every pixel assertion that reads `theme.rgb("rule")` at
the band's rule row (the old line was `C_LINE = (60, 60, 70)`, not the
palette's `(36, 64, 92)`) fail on the old tree and pass now.

### The application column: one grid definition, and the 15-70-15 bands (this increment)

`DISPLAYD_FAKE_FB=1 python3 /tmp/band_evidence.py` — a real daemon on a
loopback ephemeral port, temp policy/feedback paths, real HTTP at
1920x1080, `POST /layout {"preset": "15-70-15"}` with no `views` map (the
preset's own defaults):

    daemon: DISPLAYD_FAKE_FB=1 http://127.0.0.1:50234
    display: {'width': 1920, 'height': 1080, 'bpp': 32, ...}

    == 15-70-15 with the preset defaults ==
      left   picker  rect=(0, 0, 288, 1080)      updated=True
      center row     rect=(288, 0, 1344, 1080)   updated=True
      right  picker  rect=(1632, 0, 288, 1080)   updated=True
      left band: 7 view(s) -> columns=1 tiles=[(32, 48, 224, 113), (32, 169, 224, 113)]
        tile w/h=(224, 113)  label size at that width=20
        the old fixed three columns would be: w/h=(69, 276) label size=12
        every tile at its own palette colour inside the band: True (mismatches=[], fewest fill px=5052)
        touch regions over the band: 7, rect[0]=[32, 48, 224, 113]
          (same numbers the tiles drew: True)
        action of the first region: {'name': 'select_view', 'view': 'chat'}
      right band: (identical to the left band)
      left/right bands identical: [True, True]
      preset=15-70-15 first_pixel_ms=8.5

    == the full-panel picker is unchanged ==
      six default views -> columns=3 rect[0]=(179, 59, 508, 401)
      every tile at its own palette colour: True (fewest fill px=44833)
      switch=picker first_pixel_ms=29.6

    == a 288px band with an explicit cols param ==
      cols=3 forced -> columns=3 rect[0]=(8, 8, 85, 528)
      every tile at its own palette colour: True (fewest fill px=9839)

    == component constants ==
      ui.grid: TILE_CAP=48 COL_CAP=6 GUTTER_MIN=8 GUTTER_DIV=45
      theme band=(11, 13, 19) page=(7, 8, 12)

Reading it: the two 288px bands really carry the applicable set (7 views:
`chat`, `clock`, `html`, `options`, `picker`, `row`, `solid`) as ONE
column of 224x113 tiles — a 20px label instead of the old 12px floor on a
69px sliver — and the rects `picker_regions()` generates for that band are
the same four numbers the tiles drew, so they are tappable. The fill was
counted rather than probed because the always-on home badge covers the
left band's first tile by design (a probe there reads the badge).
`cols: 3` on the same 288px rect reproduces the old cramped shape, so the
param really does override the derived count. The full-panel picker is
byte-identical to before (three columns, `(179, 59, 508, 401)`). Frames:
`/tmp/band-evi-15-70-15-1920x1080.png`,
`/tmp/band-evi-picker-full-1920x1080.png`,
`/tmp/band-evi-picker-cols3-1920x1080.png`.

The required 1920x1080 style sweep was re-run after the change
(`DISPLAYD_FAKE_FB=1 python3 /tmp/panel_evidence.py`): all four styles
render with both badges present, first pixel 3.8-11.0ms (budget 100ms),
the html/status template view shows no error card, and the three migrated
cards are unchanged (`notice` critical bar `(214, 74, 74)` = the `alert`
role).

### The grid assertions fail before, pass after (this increment)

    $ git show HEAD:renderers/ui/grid.py
    fatal: path 'renderers/ui/grid.py' exists on disk, but not in 'HEAD'

    $ git show HEAD:renderers/_options_grid.py | grep -n 'rows = (count\|cw = max\|// 45\|return \[(rx'
    89:    rows = (count + cols - 1) // cols
    91:    g = max(8, min(rw, rh) // 45) if gutter is None else max(0, int(gutter))
    92:    cw = max(1, (rw - (cols + 1) * g) // cols)
    94:    return [(rx + g + (i % cols) * (cw + g),

    $ python3  # HEAD's own grid_geometry, exec'd from git show
    HEAD band (288px) count=8 -> (32, 48, 69, 276) cols=3
    HEAD full panel count=8 -> (179, 59, 508, 261) cols=3  (shape ignored)
    HEAD band count=8  cols passed as 1 -> (32, 48, 69, 276)   # the arg was IGNORED
    HEAD count=100000 rects -> 100000
    NOW  band count=8 -> (32, 48, 224, 98) cols=1
    NOW  full panel count=8 -> (179, 59, 508, 261) cols=3
    NOW  count=100000 rects -> 48

So on the pre-change tree `tests/test_grid.py` cannot even import (no
`ui/grid.py`); `test_a_narrow_band_is_one_application_column`,
`test_the_derived_count_keeps_a_box_roughly_square`,
`test_explicit_cols_reaches_the_touch_regions` and
`test_the_band_tile_leaves_room_for_its_label` are red against HEAD's
numbers above; `test_the_count_is_bounded` is red (100,000 rects); and
`test_neither_surface_restates_the_grid_arithmetic` names both surfaces
(the four lines above). The figures also show the change is targeted:
the full-panel grid is identical before and after.

### The accent is one owner (this increment)

`DISPLAYD_FAKE_FB=1 python3 /tmp/accent_evidence.py` — real daemon on a
loopback ephemeral port, temp policy/feedback paths, real HTTP:

    == daemon ==
    display: {'width': 1920, 'height': 1080, 'bpp': 32, 'stride': 7680, ...}  version: 0.8.0

    == the advertised accent is the palette slot ==
      activity           advertised=#50dc78 slot=#50DC78 ok
      chat               advertised=#7fd1ff slot=#7FD1FF ok
      clock              advertised=#4dc3ff slot=#4DC3FF ok
      macbook            advertised=#4da3ff slot=#4DA3FF ok
      notice             advertised=#5ac8ff slot=#5AC8FF ok
      options            advertised=#9cc8ff slot=#9CC8FF ok
      picker             advertised=#7bdff2 slot=#7BDFF2 ok
      reload             advertised=#50dc78 slot=#50DC78 ok
      retro_grid         advertised=#ffd23f slot=#FFD23F ok
      row                advertised=#5cff9d slot=#5CFF9D ok
      stream             advertised=#ff4d4d slot=#FF4D4D ok
      text               advertised=#ffffff slot=#FFFFFF ok
      touch_confidence   advertised=#50dc78 slot=#50DC78 ok
      unified            advertised=#7bdff2 slot=#7BDFF2 ok
      14 slotted view(s) advertised; mismatches: 0

    == the playlist bar paints the token (clock -> row) ==
      clock  bar row(,1076): token (77, 195, 255) present=True  filled=1003px (52%)
      row    bar row(,1076): token (92, 255, 157) present=True  filled=1074px (56%)

    == a view that draws its own accent reads the token (reload) ==
      reload bar px(x,9) all == theme.accent_rgb('reload') (80, 220, 120): True
      bar colour count: 48

    == layout styles at 1920x1080 (system buttons over every one) ==
      full                   style=full                 first_pixel_ms=3.1 badges=(True, True)
          regions: view=(0, 0, 1920, 1080)
      split-50-50            style=split-50-50          first_pixel_ms=3.0 badges=(True, True)
          regions: top=(0, 0, 1920, 540), bottom=(0, 540, 1920, 540)
      split-50-50-columns    style=split-50-50-columns  first_pixel_ms=3.3 badges=(True, True)
          regions: left=(0, 0, 960, 1080), right=(960, 0, 960, 1080)
      15-70-15               style=15-70-15             first_pixel_ms=5.9 badges=(True, True)
          regions: left=(0, 0, 288, 1080), center=(288, 0, 1344, 1080), right=(1632, 0, 288, 1080)

    == system buttons over a template view (html/status) ==
      home+sleep badge tiles on the frame: (True, True)
      error card on the frame: False (False = a real template render)
      accent px: 1998  panel px: 29852  badge px: 29416
      first_pixel_ms=6.1

Reading it: all fourteen slotted views advertise exactly their palette slot
(the sixteen deleted literals were byte-identical, so nothing moved), the
playlist bar really paints `theme.accent_rgb(<view>)` for two different
views, `reload`'s own accent bar is the token to the pixel, every layout
style renders 1920x1080 in 3–6 ms with both badges over it, and the html
status template is a real render (no error card) with the buttons over it.
Frames saved: `/tmp/accent-evi-*.png`.

### The accent assertions fail before, pass after (this increment)

    $ git grep -c '^ACCENT = ' HEAD -- 'renderers/*.py'
    HEAD:renderers/activity.py:1      HEAD:renderers/notice.py:1
    HEAD:renderers/chat.py:1          HEAD:renderers/options.py:1
    HEAD:renderers/clock.py:1         HEAD:renderers/picker.py:1
    HEAD:renderers/macbook.py:1       HEAD:renderers/reload.py:1
    HEAD:renderers/macbook_glance_color.py:1  HEAD:renderers/retro_grid.py:1
    HEAD:renderers/row.py:1           HEAD:renderers/stream.py:1
    HEAD:renderers/text.py:1          HEAD:renderers/theme.py:1
    HEAD:renderers/touch_confidence.py:1  HEAD:renderers/unified.py:1
    HEAD:renderers/unified_dock.py:1

    $ git show HEAD:renderer_registry.py | sed -n '78,90p'
        found[getattr(mod, "NAME", name)] = {
            "module": mod,
            ... "capability": capability.coerce(getattr(mod, "CAPABILITY", None)),
        }        <- no accent_slot key at all

    $ git show HEAD:playlist_color.py | sed -n '58,64p'
        for candidate in (item_color,
                          getattr((renderer_entry or {}).get("module"), "ACCENT", None)
                          if isinstance(renderer_entry, dict) else None,
                          default):

    $ git show HEAD:renderers/notice.py | grep -n '^ACCENT'   -> 29:ACCENT = "#5AC8FF"
    $ git show HEAD:renderers/text.py   | grep -n '^ACCENT'   -> 22:ACCENT = "#FFFFFF"

So on the pre-change tree: `test_no_shipped_renderer_declares_its_own_accent`
names sixteen offenders; `test_the_accent_is_resolved_from_the_palette_by_the
_registry` raises `KeyError: 'accent_slot'` on the first entry;
`test_the_views_that_draw_their_accent_read_the_palette` fails on `reload`,
`touch_confidence`, `unified_dock` and `macbook_glance_map`; and
`test_panel.py`'s tightened `assertEqual(HEX_RE.findall(src), [])` /
`assertNotIn("ACCENT", src)` fails on `notice` and `text` (and both new
`tests/test_unified.py` / `tests/test_macbook_preview.py` assertions would
only pass on HEAD because `dock.ACCENT`/`glance.ACCENT` still existed —
those two are pins on the new owner, not new-behaviour proofs).

## Note on the stop condition

The command above exits zero, but that is a **floor, not the finish line**:
the gate is a ratchet with 19 exemptions, most views still hand-draw, and
the component vocabulary exists (`system_buttons`, `ui.tile` + `ui.grid`,
`ui.shell` + `ui.stat` + `ui.text`, `ui.panel`, and the `ui.base.chain`
primitive) but the larger views have not been migrated onto it. What is
left is those migrations, the control page's style picker and the
layout-mode tap entries. The stop condition became reachable because the
gate exists and the health gate stays green while the migration is in
flight — which is exactly what it was designed to allow.
