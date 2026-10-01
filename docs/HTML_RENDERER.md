# HTML renderer (optional, litehtml)

A view that needs real layout, hierarchy and typography should be a few dozen
lines of HTML and CSS in a file, not a few hundred lines of PIL. The `html`
renderer is that escape hatch. [litehtml](https://github.com/litehtml/litehtml)
does the layout; Pillow still draws every pixel.

It is **opt-in and optional**. The daemon runs exactly as before without it, and
the view always loads even when the native library has never been built, because
"not built yet" is something the panel should be able to say.

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
| `renderers/html.py` | the view: params, the poll loop, error cards |
| `renderers/_html_templates.py` | the trust boundary: name, root, escaping |
| `renderers/_html_native.py` | ctypes + Pillow; fonts, images, clipping, tiling |
| `renderers/native/displayd_html.h` | the C ABI between them |
| `renderers/native/pil_container.cpp` | the litehtml container |
| `tools/build_litehtml.sh` | pinned build |
| `tools/bench_html.py` | the cost harness above |
| `tests/test_html.py` | the whole contract |

The renderer is not in the default picker or home screen: it needs a template
name, so it is something to `POST /show` (or wire a touch region to), not
something that appears on its own.
