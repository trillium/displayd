"""The component layer: the one place the panel's pixels are drawn.

Every surface on this panel is assembled from a small, deliberate set of
components. This package owns them, and it is the *only* place a Pillow
drawing primitive (``PIL.ImageDraw``) is allowed to appear -- enforced by
``tools/check-components.py``. A view that draws its own pixels is a
defect this package exists to delete, exactly like a colour literal is a
defect ``renderers/theme.py`` exists to delete.

Why a layer at all:

- **One definition per component.** ``system_buttons`` is the home and
  sleep badges. Before this package they were two 200-line modules that
  each carried their own tile fill, glyph, strip width, rect, region,
  draw function and suppression list -- a duplicate by construction,
  because nothing in the codebase owned "a badge".
- **Fault isolation is structural.** ``base.chain`` composes components
  and skips a failing one, so a broken component can never blank the
  panel. That guarantee used to live inside the home-chrome module, i.e.
  inside one component; it belongs to the layer that composes them.
- **Both rendering paths can ask for the same component.** A component
  is a definition (geometry + colour roles + semantics), not a drawing
  technique: the Pillow half of a component draws a frame, and a
  template half can express the same component as markup. The tokens for
  it come from ``renderers/theme.py`` either way.

Layout of the package:

- ``base`` -- the composition primitive (``chain``), no pixels.
- ``system_buttons`` -- the home and sleep badges: one module, one
  definition, composable by every view regardless of which path it uses.
- ``text`` -- one font resolution, one measurement, one fitting rule, so
  a line of type is asked for rather than re-implemented per view -- and
  ``wrap``, the one way a paragraph is broken to a width.
- ``shell`` -- the band a full-panel view wears: title, detail, health
  dot and its honest age, the rule under it, the footer line, and
  ``short_age``/``age`` (the one age-bucket rule).
- ``stat`` -- a label plus a value line (``row``), one horizontal list
  entry (``list_row``: status dot, name, right-aligned value), a
  supporting ``body`` line, and the ``meter`` bar for a fraction of a
  whole.
- ``tile`` -- a bounded box with a label: the declared-box rule, the label
  fit and centring, plus a markup half (``cell``/``layer``, what the
  picker and options fill their raw slot with) and a drawing half
  (``draw``, for the Pillow views). Its look is authored once in the
  shared stylesheet, from palette tokens.
- ``grid`` -- a grid of tiles: how many columns a region takes (read off
  its shape when the caller does not state one, so a narrow band is ONE
  application column) and where each box lands. The picker and the options
  name grid both ask it, so the two can no longer disagree about where a
  box goes.
- ``panel`` -- a titled region with a body: the one fit rule (the whole
  block scaled to the region, never truncated), a centred headline and
  body, the corner tag and the accent bar across the top.

Adding a component: give it its own module here, take every colour from
``theme``, return the frame unchanged on any failure, and add it to the
check-components exemption boundary (the layer is exempt by location, so
nothing else is needed).
"""
