"""How much of the panel a view can render into: one small vocabulary.

A renderer declares its own capability in its module, next to ``NAME``::

    CAPABILITY = "partial"

Three terms, deliberately few:

``full``
    full panel only. This is the default: a view that has not said it can
    be reduced cannot be reduced -- guessing the other way is how a
    fullscreen frame lands in a 288px band.
``partial``
    renders into a reduced region (a split half, a side band) as well as
    the whole panel.
``primary``
    ``partial``, and may hold a preset's primary region -- the centre of
    the ``15-70-15`` bands, where the view carrying the work sits.

Declaration flows one way. ``renderer_registry.load_renderers`` normalizes
it onto the entry, ``GET /renderers`` publishes it, ``layout.parse_layout``
is the sole authority on fit (a ``full`` view in a reduced region is
rejected by name), and ``layout_presets`` offers each slot only the views
that fit it via ``offered()``.
"""

FULL = "full"
PARTIAL = "partial"
PRIMARY = "primary"
ORDER = (FULL, PARTIAL, PRIMARY)
# What may render into a region smaller than the panel. A primary-capable
# view is a reduced-capable one that a preset may also make the centre.
REDUCED = (PARTIAL, PRIMARY)
UNDECLARED = FULL


def coerce(value):
    """One declaration -> one vocabulary term.

    Tolerant of case and surrounding space; anything else -- missing,
    misspelled, wrong type -- is ``FULL``. A bad declaration must fail in
    the safe direction: the panel keeps a whole frame rather than a
    cropped one."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ORDER:
            return text
    return UNDECLARED


def reduced_ok(value):
    """May this view render into a region smaller than the panel?"""
    return coerce(value) in REDUCED


def offered(renderers, allow=REDUCED):
    """The view names a layout slot may be offered, sorted.

    A view is offered when it is loaded (not broken), needs no params
    (a preset has none to give it), and its declared capability is in
    ``allow``. Unknown entries and garbage are simply not offered: this
    feeds a *choice*, so the safe failure is an absent option.
    """
    names = []
    for name, entry in (renderers or {}).items():
        if not isinstance(name, str) or not name or "/" in name:
            continue
        if not isinstance(entry, dict) or "module" not in entry:
            continue
        schema = entry.get("params") or {}
        if any(isinstance(spec, dict) and spec.get("required")
               for spec in schema.values()):
            continue
        if coerce(entry.get("capability")) in allow:
            names.append(name)
    return sorted(names)


def check_fit(regions, renderers, width, height):
    """Reject a full-panel-only view placed in a reduced region.

    ``regions`` are bound layout regions (``name``/``renderer``/``rect``),
    the shape ``layout.parse_layout`` produces; this is the sole authority
    on the rule, so every caller -- the raw grammar and the named presets
    alike -- is held to it.

    Only a *declared* ``full`` capability is refused. An entry built by
    hand (tests, embeddings) with no capability is trusted as flexible,
    because renderer_registry is where a loaded view's declaration is
    normalized -- and an undeclared loaded view defaults to FULL there, so
    a renderer author who forgets still cannot land a cropped frame.
    Raises ValueError naming the region and the view.
    """
    if (len(regions) == 1
            and tuple(regions[0]["rect"]) == (0, 0, width, height)):
        return
    for region in regions:
        entry = (renderers or {}).get(region["renderer"])
        declared = entry.get("capability") if isinstance(entry, dict) else None
        if declared is not None and not reduced_ok(declared):
            raise ValueError(
                "region %r: renderer %r is %s-only and cannot render in a "
                "%dx%d region" % (region["name"], region["renderer"],
                                  coerce(declared), region["rect"][2],
                                  region["rect"][3]))
