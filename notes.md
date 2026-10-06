# Coalescing the displayd UI around a component layer

Working notes for the overnight run. **This file is updated each increment;**
the sections below state what exists now and what is still owed, with the
reason. See also `docs/HTML_RENDERER.md`, which owns the template contract.

## What exists after this increment: the token layer

`renderers/theme.py` is the one module that owns colour. It is a table of
roles, not per-view constants:

- surfaces: `page`, `band`, `panel`, `pane`, `edge`, `rule`, `badge`
- type: `ink`, `ink-strong`, `ink-soft`, `muted`, `muted-soft`, `faint`
- state: `accent`, `alert`, `alert-ink`, `alert-body`, `alert-page`,
  `attention`, `ok`
- identity: `theme.ACCENT_SLOTS`, one accent per view name

Both rendering paths consume it, each in its own idiom:

- **Templates** get it as CSS custom properties. `_html_templates.load()`
  calls `_html_compose.compose()`, which splices one generated
  `<style>:root{...}</style>` block (palette plus per-view accent slots)
  into the document head. Verified, not assumed: the pinned litehtml
  revision implements css-variables-2 and inherits custom properties, and
  `tests/test_theme.py` renders a `<div>` with `background: var(--accent)`
  to prove the resolution happens. An undefined name is a *dropped
  declaration*, so the palette is load-bearing rather than decorative.
- **Pillow renderers** import `theme` and read `theme.rgb("<role>")` at
  draw time; `rgb()` is the only conversion point.

Migrated in this increment: `html-templates/layout.html` (the shared panel
chrome) and `html-templates/status.html` (the seed example), pixel-neutral
except for the deliberate unification of the second near-black background
and two grey steps (`#0b0d10` -> `page`, plus `ink`/`muted`/
`muted-soft` in `status.html`). On the Pillow side, the badge tile and
glyph in `renderers/home_chrome.py` / `renderers/sleep_chrome.py` (six
duplicated literals deleted) and the whole failure card in
`renderers/_html_error.py` (the alert family) now read the palette.

`tests/test_theme.py` pins: the palette is complete and deterministic, an
unknown token raises, every document round-trips through the token block,
a migrated template's style carries **no** `#rrggbb` literal, and the
shared chrome really paints those tokens at 1920x1080.

## Recorded evidence so far

Real headless daemon (`DISPLAYD_FAKE_FB=1`, ephemeral port, temp policy/
feedback paths), showing the tokenised template view with both system
buttons composited over it:

    $ python3 - <<'PY'   # boots displayd.py --bind 127.0.0.1, POST /show
    ...                  # {"renderer": "html"}, GET /snapshot, census
    show: 200
    state renderer: html | html.ok: True | templates: 6
    page        (7, 8, 12)     True
    band        (11, 13, 19)   True
    rule        (36, 64, 92)   True
    accent      (127, 209, 255) True
    badge       (13, 17, 28)   True
    ink-strong  (255, 255, 255) True
    frame size: (1920, 1080)

So the palette drives the chrome from the template side, and the two
system buttons (which are still Pillow overlays) read the same palette
from `theme.py` in the same process. The frame is saved at
`/tmp/panel-html-tokens-1920x1080.png` for the run's screenshots.

Test suites:

    $ python3 -m unittest tests.test_theme -v            -> Ran 21 tests, OK
    $ python3 -m unittest tests.test_picker tests.test_unified \
        tests.test_chat tests.test_html tests.test_html_runtime_install \
        tests.test_control tests.test_options tests.test_layout
                                                          -> Ran 304 tests, OK
    $ python3 tools/check-lines.py                        -> line budget ok
    $ python3 tools/check-repo-health.py                  -> exits 0

The `var()` resolution was confirmed to *fail before* the change (a box
with `background: var(--accent)` renders the page colour without the
injected block, accent blue with it), which is the shape every pin here
takes. `tools/check-components.py` does not exist yet, so the objective's
full stop condition cannot pass until the component-layer increments land.

## Still owed (with the reason)

- **The component layer** (`system_buttons`, `shell`, `tile`, `panel`,
  `stat`) and the merge of `home_chrome` + `sleep_chrome` into one
  `system_buttons` module. The token layer lands first because the chrome
  components style themselves from it; merging the two button modules
  before the palette existed would have moved duplicated literals from one
  file to another.
- **The template composition step for chrome** (`<!--#include ...-->`
  expansion at load time) so the ten shared chrome bands are authored
  once. The token block proved the compose step and its test story
  (`strip_tokens`) before the include directive was added.
- **Layout presets** (`full`, `split-50-50`, `split-50-50-columns`,
  `15-70-15`) on top of the existing `POST /layout` grammar.
- **The full/partial renderer capability** declaration surfaced through
  `GET /renderers`.
- **`tools/check-components.py`**, the structural gate, and its wiring
  into `tools/check-repo-health.py`. It must let the health gate stay
  green while the migration is in flight, so it starts as a ratchet: an
  explicit exemption list that may only shrink, with a stale exemption
  failing the gate.
