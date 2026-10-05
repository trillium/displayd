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

    POST /show {"renderer": "html",
                "params": {"template": "status.html",
                           "vars": {"title": "BUILD", "sub": "12:04",
                                    "value": "142", "unit": "taps",
                                    "note": "AIM review - last 7 days"}}}

    POST /feed/html/vars {"title": "...", "value": "..."}   # re-renders in place

## layout.html: the shared panel chrome

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

The only raw value the panel ever passes is `renderers/_picker_tiles.py`'s tile
markup: fixed palette, fixed arithmetic, and a view name escaped before it
reaches the document.

### The picker now needs the engine

The picker was a pure-Pillow view; it is now a template surface, so on a target
with no built engine it shows the red "build it: tools/build_litehtml.sh" card
instead of tiles. That is the same deal as the `html` view and the same
mitigation: `install.sh` refuses to install without the runtime
(`install_html_runtime.sh --strict`) and `deploy.sh` builds it on the host. A
host with no C++ toolchain therefore degrades loudly rather than silently --
which is worth knowing before a deploy lands on a fresh machine, because the
home screen is the first thing anyone taps.


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

### Two litehtml behaviours worth knowing before you write a template

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
| `renderers/_picker_tiles.py` | geometry -> tile markup + chrome variables |
| `renderers/_html_templates.py` | the trust boundary: name, root, escaping, raw slots |
| `renderers/_html_error.py` | the red rule and its message, shared by both views |
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
while `renderers/_picker_tiles.py` turns geometry into that layer. Nothing
draws in Pillow on that path any more, which is also why the failure card lives
in `renderers/_html_error.py`: both views share one.
