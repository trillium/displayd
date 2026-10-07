# HTML renderer (optional, litehtml)

A view that needs real layout, hierarchy and typography should be a few dozen
lines of HTML and CSS in a file, not a few hundred lines of PIL. The `html`
renderer is that escape hatch. [litehtml](https://github.com/litehtml/litehtml)
does the layout; Pillow still draws every pixel.

It is **optional**: no other view depends on it, the daemon runs exactly as
before without the engine, and the view always loads even when the native
library has never been built, because "not built yet" is something the panel
should be able to say.

## Build

The engine is not vendored. One pinned commit, built by one script:

    DISPLAYD_LITEHTML_BUILD="$PWD/build/litehtml" ./tools/build_litehtml.sh

That writes `renderers/native/liblitehtmlpil.dylib` (`.so` on Linux) and
refreshes the two licence notices next to it. `./tools/build_litehtml.sh --check`
reports whether the built library is present and current.

Pinned: litehtml `5624e795be50f02c21c89985c374dcd659dbd74b` (BSD-3-Clause) and
gumbo (Apache-2.0), both in `renderers/native/`. `lhtml_version()` reports the
revision, so a deployed panel can be asked which engine it is running.

The library is loaded lazily, on first render, not at import. So a checkout
without it still discovers the renderer, still lists it in `GET /renderers`, and
shows a card explaining how to build it.

## Installing

A clean target needs five things for this view to work: the two licence
notices, the three C++ sources, `tools/build_litehtml.sh`, at least one
`html-templates/*.html`, and the built engine itself. One script owns that set,
so "did it all arrive?" is one question with one answer:

    ./tools/install_html_runtime.sh --prefix /opt/displayd   # install (builds if needed)
    ./tools/install_html_runtime.sh --prefix /opt/displayd --check   # verify, never builds
    DISPLAYD_HTML_LIB=/path/to/liblitehtmlpil.so            # override the library

`install.sh` runs it `--strict` before it writes the unit file, so a panel that
advertises the view cannot be installed without its runtime (`DISPLAYD_SKIP_HTML=1`
opts out). `deploy.sh` runs the same script on the host after the rsync and
before the restart, **without** `--strict`: the engine is a Linux build, so it is
produced on the target by the shipped pinned build script, and a host with no
C++ toolchain must degrade to a loud warning rather than fail a deploy of an
otherwise healthy panel. Both are no-ops once the engine exists.

The result is machine-readable on stdout in every mode:

    complete: <prefix>        # every required artifact is in place
    missing: <relative path>  # one line per gap, both modes
    incomplete: <prefix>      # trailing verdict when anything was missing

`GET /state` carries the same answer as the `html` key
(`{"ok": true, "library": ..., "version": ..., "template_root": ..., "templates": [...]}`),
so a deployed panel can be asked what it has without anybody reading logs.

## Templates

Templates live in one directory, and the request never chooses it:

    $DISPLAYD_HTML_TEMPLATES       # if set
    <repo>/html-templates/         # otherwise, next to the daemon

A name is `[A-Za-z0-9_-]{1,64}.html`. No path separators, no traversal, no
extension tricks, no nested directories -- a template is one file in the root.
Copy `html-templates/status.html` as the starting point; it is a working
example, not a stub.

A file whose name starts with `_` is a composition **partial**
(`_chrome.html`, the shared chrome), not a template: `available()` never
lists it and a caller cannot name it. Templates pull sections of it in with
an include directive -- see "One chrome, composed in Python" below.

    POST /show {"renderer": "html",
                "params": {"template": "status.html",
                           "vars": {"title": "BUILD", "sub": "12:04",
                                    "value": "142", "unit": "taps",
                                    "note": "AIM review - last 7 days"}}}

    POST /feed/html/vars {"title": "...", "value": "..."}   # re-renders in place

## layout.html: the shell surface

`html-templates/layout.html` is the shell the UI is built from: header,
side gesture strips, content band, footer. It is the **default template**,
so `template` is an ordinary optional param and this view is selectable,
rotatable and tileable like any other -- a bare

    POST /show {"renderer": "html"}

draws the shell, and a later

    POST /feed/html/vars {"eyebrow": "NOW", "title": "BUILD", ...}

becomes its live content without a re-show. `template` and `vars` are
otherwise unchanged, and the trust boundary below applies to this template
exactly as it does to every other.

The chrome it wears is not authored here: the bands, the gesture strips and
their rules come from `html-templates/_chrome.html` (see "One chrome,
composed in Python" below), which this file -- and picker, options and chat --
splices in. This file owns the shell's two content bands, `lead` and `body`,
and the variant class on its `<body>`.

### The variable contract

All ten are **required**: a placeholder with no value is an error, not a
blank, so a partial push names what is missing instead of drawing a shell
that looks right and is wrong. An empty string is a real value and
collapses its slot, so a caller hides a band by emptying it. The set is
pinned by a test against the shipped `DEFAULT_VARS` in `renderers/html.py`,
so the template and its defaults cannot drift apart.

| Variable | Slot |
| --- | --- |
| `eyebrow` | short label above the title, dim |
| `title` | the name of what is on screen, large |
| `status` | right-hand state (clock, health), dim |
| `lead` | the line that must read from across a room |
| `body` | supporting detail under the lead |
| `hint_left` | label in the left gesture strip |
| `hint_right` | label in the right gesture strip |
| `footer` | small bottom-left status |
| `footer_right` | small bottom-right status |

Authored at 1920x1080 for a panel read from across a room: type is large
and bands are wide. litehtml lays the frame out to whatever viewport it is
handed, so the same file also fills a smaller screen, but the type size is
fixed in px -- litehtml has no viewport-relative units.

One documented exception to "required": an **empty** variable set on the
default template renders `DEFAULT_VARS` from `renderers/html.py`, because a
tile that shows a red card the moment it is tapped is not a usable home
tile. Every other template, and every partial push, still names its missing
key.

## One chrome, composed in Python (`_chrome.html`)

litehtml has no `@import` (a CSS import is inert) and a template cannot
inherit from another document, so a shared chrome cannot be shared *in CSS*.
It is shared in Python instead: the chrome is authored **once** in
`html-templates/_chrome.html` as named sections, and a template names the
sections it wants with an include directive, which
`renderers/_html_compose.py` splices into the document at load time --
before any value is substituted.

    <!--#include css-->           the chrome stylesheet, inside <style>
    <!--#include head-->          the header band markup
    <!--#include title-->         the title band
    <!--#include subtitle-->      one dim line under the title
    <!--#include rule-->          the rule under the title
    <!--#include foot-->          the footer band
    <!--#include strip-left-->    the left gesture strip
    <!--#include strip-right-->   the right gesture strip

The file that holds them is a *partial*, not a template: it is named
`_...html`, `available()` never lists it, and a caller cannot name it (`load`
and `source` refuse a `_`-prefixed name). It ships by the same
`html-templates/*.html` install rule as the templates, so a partial travels
with them.

Failures are loud, because a silently missing chrome is a
plausible-looking wrong panel: an unknown section, a missing partial, a
nested include deeper than four levels, or a composed document over 512KiB
is a `TemplateError` that the view draws as a card.

### Variants: the only per-surface difference, as data

The surfaces genuinely differ in inset, type size and band padding. Those are
not copies of a rule -- they are custom properties a class on `<body>` sets,
with the variants declared next to the rules:

    <body class="panel">          the shell: 64px inset, 96px title, left
    <body class="panel center">   title centred, smaller lead (options.html)
    <body class="panel tight">    dense bottom band, no head pad (picker.html)
    <body class="panel wide">     176px inset, 72px title (chat.html)
    panel bright                  the rule takes var(--accent), not var(--rule)

The promise is mechanical and tested (`tests/test_html.py`,
`TestChromeComposition`): every shipped panel template includes the chrome `css` section,
every one names a variant the partial declares, and **no template restates a
selector the partial owns** -- a re-authored rule is the duplication this
step removes, and the test names the file.

## Trusted local templates only

Only a file that already exists in that root is ever read: the directive
names a *section*, never a caller's path or markup, and the partial is read
from the same root as the template that includes it. A caller never gains a
route to the composition step, so the trust boundary below is unchanged.

## Design tokens: the palette lives in one module

`renderers/theme.py` owns every colour the panel shows. It is not a
stylesheet and not a per-view constant: it is a table of roles --
| `page`, `band`, `panel`, `pane`, `edge`, `rule`, `badge`, `track`,
`ink`,
`ink-strong`, `ink-soft`, `muted`, `muted-soft`, `faint`, `accent`, the
alert family (`alert`, `alert-ink`, `alert-body`, `alert-page`),
`attention`, `ok` -- plus one accent per view under
`theme.ACCENT_SLOTS`.

Both rendering paths read that table, and they read it differently
because they are different technologies:

- **Templates** get it as CSS custom properties. `_html_compose.compose()`
is called by `_html_templates.load()`, so every document the engine lays
out carries one generated `<style>` block:

      :root { --page: #07080c; --ink: #eef2fa; ... }
      :root { --accent-clock: #4DC3FF; ... }

  A template then writes `color: var(--ink)` and nothing else. This works
  because the pinned engine implements css-variables-2 (`subst_var` in
  `style.cpp`) and inherits a custom property up the element tree; it is
  **not** a browser feature to be assumed. An undefined name is a
  *dropped declaration*, not a fallback colour -- which is why a template
  that names a token must have spelled it right, and why
  `tests/test_theme.py` renders a box to prove the resolution happens.
- **Pillow renderers** `import theme` and call `theme.rgb("badge")` for
the role they mean. `rgb()` is the one conversion point.

A view's identity accent is part of the palette too
(`theme.ACCENT_SLOTS`), not a per-view constant: `renderer_registry`
resolves the slot into every renderer entry it loads and
`playlist_color.accent_for` reads it, so the playlist bar, the renderer
listing and the picker all agree without a renderer declaring a colour.
A renderer from outside this tree may still declare its own `ACCENT`, which
takes precedence over the slot.

Which ink reads on a surface is a rule of the palette, not of the view:
`theme.ink_on(surface)` answers with `on-accent` on a bright fill and
`muted` on a dark one, so a view that paints a surface the *caller* chose
(`qr`'s card page, an operator's `background` param) can pick a legible ink
without holding a colour literal or its own brightness test.

A migrated template carries **no** colour literal: `layout.html` and
`status.html` are the first two, and `tests/test_theme.py` fails on any
`#rrggbb` in a migrated style, so a template cannot quietly re-author a
colour the palette already owns. The same file pins that the shared
chrome really paints those tokens at 1920x1080.

Two consequences worth stating plainly:

- loading a template returns the *composed* document, so the token block
  is part of what a test sees. `_html_compose.strip_tokens()` takes it
  back off, and `tests/test_html.py` asserts the substitution contract on
the text around it -- exactly, not loosely.
- `renderers/ui/system_buttons.py` (the component layer) and
`renderers/_html_error.py` already read roles from the palette (the badge
tile, the glyph, the alert family) instead of carrying their own
literals: that is the shape the remaining Pillow views migrate towards,
and it is why the two system buttons cannot drift apart.

## picker.html: the tile grid

`html-templates/picker.html` is the same chrome (header, side strips, bottom
band) plus the one thing the chrome cannot express as ten text variables: a
tile grid. `renderers/picker.py` no longer draws in Pillow. It fills the chrome
slots from the screen geometry and emits **one absolutely positioned tile div
per view**, at exactly the rects `picker.grid_geometry()` hands
`picker_regions()` -- so a drawn tile and the region that taps it are the same
four numbers by construction, not by two copies of a constant. The touch
contract is untouched: the regions, `touch-picker.json.example`, the CLI
generator and `tests/test_picker.py` all still call the same functions.

The tile itself is a COMPONENT, not a picker detail:
`renderers/ui/tile.py` owns "a bounded box with a label" -- the declared-box
compensation, the shrink-until-it-fits rule, the measured centring, and both a
markup half (`cell`/`layer`, what the picker and options fill their raw slot
with) and a drawing half (`draw`, which a Pillow view such as
`renderers/touch_confidence_draw.py` uses for its region map). Its look is
authored once in the shared stylesheet (`_chrome.html`): `.tile`/`.lab`/`.shade`
for a filled tile, `.cell`/`.name` for a plain one, every colour a token
(`on-accent`, `label-shade`), and the frame width pinned to `tile.BORDER` by
`tests/test_tile.py`. A surface that restates a tile rule fails that test.

Two litehtml facts the tile layer depends on, both verified live:

- a declared `width`/`height` is the **content** box, so a tile declares its
  rect minus the frame; without that every tile is 12px wider than the region
  that taps it;
- an absolutely positioned child offsets from its positioned parent, so a tile
  label is placed in tile-local pixels, measured with the same face the
  document text uses.

`background` and `color` reach the document as `#rrggbb` strings built from the
colours the screen already parsed, never as the caller's own text, so the
template can take them in a style attribute without opening a CSS injection.

### The raw slot: `{{name|raw}}`

A tile grid is markup, so `picker.html` declares one slot marked `raw`. It is
filled from a **separate `raw` mapping** that `load()` takes as its own
argument, and the html renderer never has one: a caller of `POST /show` or
`POST /feed/html/vars` supplies `variables`, which are escaped, so a value
named `tiles` lands in that slot as escaped text. A raw slot nobody filled is a
missing variable like any other and names itself the same way. Raw values are
refused if they carry a placeholder of their own, are not strings, or exceed
`MAX_RAW_CHARS`. `templates.RAW_RE` finds the slots; a test asserts no shipped
template declares more than one.

The only raw value the panel ever passes is `renderers/ui/tile.py`'s tile
markup (the picker builds it with `cell()` / `layer()`): fixed palette, fixed
arithmetic, and a view name escaped before it reaches the document.

### The picker now needs the engine

The picker was a pure-Pillow view; it is now a template surface, so on a target
with no built engine it shows the red "build it: tools/build_litehtml.sh" card
instead of tiles. That is the same deal as the `html` view and the same
mitigation: `install.sh` refuses to install without the runtime
(`install_html_runtime.sh --strict`) and `deploy.sh` builds it on the host. A
host with no C++ toolchain therefore degrades loudly rather than silently --
which is worth knowing before a deploy lands on a fresh machine, because the
home screen is the first thing anyone taps.

## dock.html: the apps dock

`html-templates/dock.html` is the strip `renderers/unified_dock.py` composites
under the merged home screen. It is a document in its own right rather than a
chrome template, because a strip is a strip: a head row (live dot + `MAC APPS`,
mode line at the right), one dim subtitle, one big line, one tail row (the
LEFT/RIGHT split, the tap hint at the right). The slot names are the shared
chrome's anyway -- `eyebrow`, `status`, `subtitle`, `title`, `body`,
`footer_right` -- so the file reads like `layout.html`. Five more carry the
colours: `background`, `color`, `dim`, `accent` (live green), `alert` (stale
amber), `line`, plus `marker`, the stale marker in front of the title, which is
**empty** when the feed is live so it collapses instead of leaving a gutter.
All thirteen are required, and there is no `{{name|raw}}` here at all.

Two things differ from the other templates, both deliberate:

- **it is not drawn at panel size.** It is authored at `dock.DESIGN`
  (1760x230, the rect `unified.default_dock()` derives at 1920x1080) and the
  renderer scales the rendered strip to whatever rect the caller passed, so a
  custom dock keeps the same type at the same relative size instead of
  overflowing the way a fixed-px document would.
- **its failure is a strip, not a card.** The dock is pasted into a finished
  frame, so `error_strip()` in `renderers/_html_error.py` (the card component
  confined to a rect) draws the alert rule and the message *inside the dock
  rect only* -- a full-screen card would hide the tiles around it, and a
  silently missing strip would be indistinguishable from a home screen that
  simply has no apps.

litehtml parses `border-radius` and passes the per-corner radii through to the
container, which displayd's `renderers/native/pil_container.cpp` carries across
the C ABI to the PIL painter -- so the strip's outline and the head "dot" are
rounded (`border-radius` in `dock.html`: 28px on the outline, 50% on the dot).
The radii stay eight independent values: the painter builds an anti-aliased
coverage mask per box in `renderers/ui/radius.py`, cached on
`(width, height, radii)` because that tuple is the mask's only input.
Everything else -- three
states
(no payload yet / live / quiet past `STALE_AFTER`), the count, the focused app,
the overflow, the split, the mode line -- is the behaviour the Pillow version
had, and `tests/test_unified.py` pins it against rendered pixels, including
that the strip only ever paints inside the rect the `apps-dock` tap region
targets.

## options.html: the selection surface

`html-templates/options.html` is the tap-anywhere landing screen
(`renderers/options.py`) -- the target of a tap that no configured touch region
consumed, on every fullscreen view. It is the shared chrome from `layout.html`
(its `eyebrow`/`status`/`title`/`subtitle`/strip/footer slots) plus **one**
addition: a name layer. A two-column grid of view names cannot be expressed as
ten text variables, so the renderer absolutely positions one div per name at the
exact rects `renderers/_options_grid.grid_geometry()` hands out -- the drawn
name and the geometry that places it are the same four numbers by construction,
so they cannot drift.

It deliberately carries **no per-name tap region**: options NAMES the picks and
the way back, the picker SELECTS, and either can target the other without
trapping the user. `options_regions()` returns `[]` explicitly, so "what does a
tap here do?" has a documented answer rather than an `AttributeError`.

| Variable | Slot |
| --- | --- |
| `eyebrow` | `DISPLAYD`, small label above the title |
| `title` | the view's `title` param, the biggest type |
| `status` | `N VIEWS`, the right-hand state |
| `subtitle` | the `instructions` param, one dim line under the title |
| `hint_left` / `hint_right` | labels in the two gesture strips |
| `footer` / `footer_right` | small bottom status |
| `background`, `color`, `dim`, `accent` | colours, `#rrggbb` strings the renderer parsed out of params, never raw caller text, so a style attribute cannot be injected |
| `{{names|raw}}` | the name layer -- see below |

`{{names|raw}}` is the one raw slot on this surface, and it is filled from a
separate mapping the html view never passes: only `options.py` writes it, and
what it writes is name divs generated in-process from `grid_geometry()` plus
names that were escaped before they went in. A caller-supplied `views` entry can
therefore never add markup -- it is the only value here that comes from outside
the repo, and `tests/test_options.py` pins that it arrives escaped.

Two bounds are deliberate. Names are truncated at `_options_grid.MAX_CELLS`
(48) in `coerce_views()`, so the header's count and the drawn cells always
agree; and `grid_geometry()` is total and bounded on *any* input, because it
allocates a rect per name -- the old Pillow loop shrank the row height instead,
so a huge list cost nothing there and would be a runaway allocation here. The
label size shrinks to fit each cell and the label box is measured from the
real face (`fit_size()` / `label_box()` in `renderers/ui/tile.py`, the tile
component options and the picker share), because litehtml does not centre a
label the way a browser would.

Failure here is the shared full-screen card, not a strip: options is a whole
panel view, so a deleted template leaves the red rule and a message that names
the missing key.

## chat.html: the two-pane chat panel

`html-templates/chat.html` is the chat view (`renderers/chat.py`): the shared
chrome, minus the side gesture strips (the chat panel has no tap regions), plus
one two-pane layer. Left is the roster of viewers in the channel now, fed by
`POST /feed/chat/roster`; right is the chat, fed by `POST /feed/chat/message`
with messages and join events on the same ordered stream. With nothing pushed
there is no pane layer at all and the notice says the panel is waiting -- the
healthy idle state (project-a4t.8.1) -- rather than framing an empty roster
column and silence.

An empty value collapses a chrome slot, as everywhere else. The layer itself is
one raw slot, `{{panes|raw}}`, and it holds both pane divs: a document gets one
raw slot, so the panes' boxes and rows are generated together by
`renderers/chat_panes.py`, which measures each line with the same face the
document draws with (`renderers/chat_fit.py`) and places it at panel pixels,
the way `renderers/ui/tile.py` places tiles. Every display name and every message
body is escaped before it reaches that markup, and the only colours in it are
`#rrggbb` strings built from palette tuples the screen already parsed, so a
pushed chat payload can never add markup or a style.

Presence is never fetched by this view. The bridge polls Firebot once per cycle
and pushes the roster and the joins off that single read
(`bridges/firebot_roster.py`), so the daemon stays output-only and the join
diff and the pane cannot disagree; when the pushes stop, the panel says
`ROSTER STALE` with the age instead of showing last-known presence as live.

Like `picker`, `options` and the home screen, this view needs the built engine:
without it the panel shows the red "build it: tools/build_litehtml.sh" card
instead of the chat.


## The trust boundary

This is the whole design, so it is worth being precise about.

**A caller never supplies markup or CSS.** A caller supplies a *file name* that
already exists on disk, plus *data*. Everything structural -- tags, styles,
image paths -- comes from a file the operator put there.

**Every `{{variable}}` value is HTML-escaped** before it reaches the document
(`&`, `<`, `>`, `"`, `'`). A feed payload is the one genuinely untrusted input
on a panel, so it cannot introduce an element, an attribute, a style, a URL, or a
script. `<img src=x onerror=alert(1)>` pushed as a value renders as those
literal characters.

**A placeholder with no value is an error, not a blank.** The panel names the
missing key instead of quietly showing a wrong dashboard. Same rule for a
template that comments itself out, for an unknown template, and for a missing
template root: all of them name themselves on screen.

**HTML comments are dropped before placeholders are read**, so a commented-out
section or a documented example in a comment costs no variables.

**Images must be local and must stay inside the root.** A reference is refused
for anything that is a URL, a `data:` URI, an absolute or home path, or a UNC
path; `?query` and `#fragment` are stripped; `.` and `sub/../` are fine as long
as the real path stays under the root; a symlink pointing out is refused. The
trusted root has to be handed through `allow_root()` first, so a symlinked
directory is resolved once and matched on its real path, not as a string prefix.

**No JavaScript, ever.** litehtml has no host for it. `import_css` is inert, so
remote CSS cannot be fetched. There is no code path to a socket, so a template
cannot cause one.

## What renders, and what does not

Supported: block and flex layout, padding and margins, absolute positioning,
solid and `rgba()` colours, borders, text, common list markers, and local
PIL-decodable images (with `background-size`, `background-position` and all
four `background-repeat` modes).

Not supported: grid, gradients, rounded clipping, video, canvas, forms,
JavaScript, webfonts, remote resources. Unsupported CSS is ignored, not
misrendered -- the panel shows the layout it does understand.

### Four litehtml behaviours worth knowing before you write a template

**A shorthand whose value contains `var()` is dropped entirely.**
`padding: 44px var(--inset) 0 var(--inset)` applies *no* padding at all --
silently, because a dropped declaration is not an error. Write the longhands
(`padding-left: var(--inset)` and friends); a single-property declaration with
a `var()` is fine. `tests/test_html.py` fails on a shorthand carrying a
`var()` in the chrome or in a panel template.

**A rule using `var()` beats an inline `style` on the same element.** The
substitution path re-applies the declaration, so a value a caller offers as an
inline property loses to the chrome's rule. That is why a surface takes its
page and ink colour by overriding the *role* (inline `--page` / `--ink` on
`<body>`) rather than the property: the chrome's one rule keeps painting, with
the caller's colour.

**`gap` is dropped on a flex row whose children grow.** This is upstream, not a
bug in this renderer, and it looks like a spacing bug rather than a missing
feature. Use `margin-right` on the children.

**`width:100px` is the content box.** Borders and padding sit outside it, so a
`width:100px` box with a `6px` border on each side is 112px wide on screen.

## Failure is always visible

Every failure -- no template root, unknown template, missing variable, template
that parses but will not draw, native library not built -- draws a red rule and
a readable message naming the problem and the fix. The view never returns
without a frame, because a blank panel is indistinguishable from a dead daemon.
After a bad push, a later good push of the same variables recovers the view in
place; it does not need a re-show.

## Cost

`python3 tools/bench_html.py` measures it. On an M-series Mac at 1920x1080 with
`status.html`:

    cold first render :    6.98 ms
    warm render       :    5.64 ms median (25 runs)

Memory is flat in steady state, which is the number that matters for a panel
that runs for days. Sampled while rendering:

       25 renders:  40224 KiB  (+9.60 KiB/render)
      100 renders:  40544 KiB  (+4.27 KiB/render)
      400 renders:  40720 KiB  (+0.59 KiB/render)
     2400 renders:  41088 KiB  (+0.18 KiB/render)

The per-render cost falls as the font and image caches fill and then flattens;
a leak would hold a constant per-render cost instead. `--sizes` also times the
pathological documents the internal caps exist for (a 1px tile over a full-HD
box, 2000 nested divs, a long run of low-alpha text), because a render budget
that only survives polite input is not a budget.

## Where the code is

| File | Role |
| --- | --- |
| `renderers/html.py` | the view: params, defaults, the poll loop, error cards |
| `html-templates/layout.html` | the shared panel chrome + its variable contract |
| `html-templates/picker.html` | the chrome plus the tile layer (one raw slot) |
| `renderers/picker.py` | the picker view: params, views, touch geometry |
| `renderers/_picker_tiles.py` | the picker's chrome variables |
| `renderers/ui/tile.py` | the tile component: box geometry, label fit, the markup half and the drawing half |
| `renderers/ui/grid.py` | the grid of tiles: the column count (shape-derived when no `cols` is given) and the row-major rects, asked by the picker and the options name grid alike |
| `renderers/ui/shell.py`, `renderers/ui/stat.py`, `renderers/ui/text.py`, `renderers/ui/paragraph.py` | the band a Pillow view wears (inset clear of the badges via `band_pad`), the label/value row, its meter and the `pill` overlay tag, the layer's one font/measure/fit rule plus `fit_size` and the anchored one-line writer, and the fitted multi-line paragraph block (`reload`'s highlights; the template path takes its band from the shared stylesheet instead) |
| `renderers/ui/panel.py` | the panel component: a titled region with a body -- one scale-to-fit rule, a centred headline and body, the corner tag and the accent bar (`notice`, `text`, `sleep`, `clock` are composers over it; `touch_confidence` composes its lines and its region boxes from `panel` + `tile`) |
| `renderers/ui/progress.py` | the progress component: a fraction of a whole across a flush edge strip (`boxes`/`shown`/`contrast`/`draw`), the playlist bar's one definition, drawn from the `track` token and the contrast border roles -- `playlist.py` composes it and does not draw |
| `html-templates/dock.html` | the apps dock strip under the home screen |
| `renderers/unified_dock.py` | dock feed state -> the strip's variables + composite |
| `html-templates/options.html` | the selection screen: chrome plus one name layer |
| `renderers/options.py` | the options view: params, the pinned picks, the card |
| `renderers/_options_grid.py` | the name grid's geometry + the chrome variables |
| `html-templates/chat.html` | the two-pane chat panel: roster left, chat right |
| `renderers/chat.py` | the chat view: params, the ordered event timeline, the loop |
| `renderers/chat_panes.py` | geometry -> pane/row/line markup + the chrome variables |
| `renderers/chat_fit.py` | measurement and wrapping: text -> measured rows |
| `renderers/_html_templates.py` | the trust boundary: name, root, escaping, raw slots |
| `renderers/_html_error.py` | the failure card: the panel card in the alert family, shared by both views |
| `renderers/_html_native.py` | ctypes + Pillow; fonts, images, clipping, tiling |
| `renderers/native/displayd_html.h` | the C ABI between them |
| `renderers/native/pil_container.cpp` | the litehtml container |
| `tools/build_litehtml.sh` | pinned build |
| `tools/install_html_runtime.sh` | the install/check set (engine, sources, licences, templates) |
| `tools/bench_html.py` | the cost harness above |
| `tests/test_html.py` | the whole rendering contract |
| `tests/test_html_runtime_install.py` | the install/check/deploy contract |

The renderer carries the default template, so it appears on the picker and
the merged home screen and rotates like any other view. It is still a
template view -- it draws no tile grid of its own -- and every other file in
the root is still one `POST /show` away.

The picker itself is now a template surface too: `picker.html` is the same
chrome plus a tile layer, and `renderers/picker.py` holds the view contract
while `renderers/ui/tile.py` -- the tile component -- turns geometry into that
layer and `renderers/ui/grid.py` decides where the boxes go. The column count
is read off the region's shape unless the caller states `cols`, so a 288px
application band (the 15-70-15 side bands) draws ONE column of 224px tiles
instead of three 69px ones, and the drawn tiles and the generated touch
regions stay the same numbers because both come from that one grid. Nothing
draws in Pillow on that path any more, which is also why the failure card lives
in `renderers/_html_error.py`: it is `ui.panel.card` (or `ui.panel.strip`,
confined to a rect of a bigger frame) in the alert family, so both views share
one definition.
