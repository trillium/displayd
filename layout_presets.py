"""Named layout styles: presets over the existing /layout grammar.

``POST /layout {"regions": [...]}`` is the grammar (``layout.parse_layout``:
stack, grid, rect). A preset is not a second layout system -- it is a named
set of region specs *in that grammar* plus the rule for which views each
slot may carry, so the common shapes need no geometry in the request::

    POST /layout {"preset": "15-70-15"}
    POST /layout {"preset": "15-70-15", "views": {"center": "row"}}

The four styles:

``full``
    one view owns the panel -- today's behaviour, no geometry.
``split-50-50``
    two equal rows.
``split-50-50-columns``
    two 50-wide columns.
``15-70-15``
    a narrow left band, a wide centre, a narrow right band: the centre is
    the primary region (a PRIMARY-capable view: the row, which Talon fits)
    and the side bands are navigation -- the tappable application list --
    over the views that can render reduced.

A slot's renderer is ``views[slot]`` when given, else the preset's default:
a primary slot prefers a PRIMARY-capable view, a navigation band prefers
``picker``, any other slot takes the best-fitting view not already used.
Which views may be *offered* is ``capability.offered`` (declared, loaded,
and needs no params); which views actually *fit* a slot is decided by
``layout.parse_layout``, which rejects a full-panel-only view in a reduced
region -- the presets ask it rather than repeating that rule.

``doc()`` is the read-only projection ``GET /layout/presets`` serves: per
style, its slots, their geometry, and the views that fit each one.
"""

import capability as caps

# A slot's role: which views it may be offered and which it defaults to.
PRIMARY = "primary"
NAVIGATION = "navigation"
PLAIN = "plain"

NAV_RENDERER = "picker"  # the tappable application list
PRIMARY_RENDERER = "row"  # Talon's streak row: the 15-70-15 centre

PRESETS = {
    "full": {
        "label": "Full panel",
        "hint": "one view owns the screen",
        "primary": "view",
        "regions": (("view", {}),),
    },
    "split-50-50": {
        "label": "Two rows",
        "hint": "two equal rows",
        "regions": (("top", {"height": "50%"}),
                    ("bottom", {"height": "50%"})),
    },
    "split-50-50-columns": {
        "label": "Two columns",
        "hint": "two 50-wide columns",
        "regions": (("left", {"width": "50%"}),
                    ("right", {"width": "50%"})),
    },
    "15-70-15": {
        "label": "Bands",
        "hint": ("narrow application bands either side of the centre "
                 "view"),
        "primary": "center",
        "navigation": ("left", "right"),
        "regions": (("left", {"width": "15%"}),
                    ("center", {"width": "70%"}),
                    ("right", {"width": "15%"})),
    },
}
PRESET_NAMES = tuple(PRESETS)


def slot_roles(preset):
    """{slot: PRIMARY|NAVIGATION|PLAIN} for one preset. Raises KeyError."""
    spec = PRESETS[preset]
    navigation = set(spec.get("navigation") or ())
    primary = spec.get("primary")
    return {slot: (PRIMARY if slot == primary else
                   NAVIGATION if slot in navigation else PLAIN)
            for slot, _geometry in spec["regions"]}


def _cap(renderers, name):
    entry = (renderers or {}).get(name)
    return caps.coerce(entry.get("capability") if isinstance(entry, dict)
                       else None)


def _candidates(role, renderers):
    """Slot-ordered candidate list: best fit first, everything that fits
    after, so a caller without a `views` map still gets a sensible panel."""
    names = caps.offered(renderers)
    if role == NAVIGATION:
        preference = [NAV_RENDERER]
    elif role == PRIMARY:
        preference = [PRIMARY_RENDERER] + [n for n in names
                                           if _cap(renderers, n) == caps.PRIMARY]
    else:
        preference = [n for n in names
                      if _cap(renderers, n) == caps.PRIMARY]
    preference = list(dict.fromkeys(preference))
    out = [n for n in preference if n in names]
    return out + [n for n in names if n not in out]


def _default(role, renderers, used):
    """The first candidate fit for the slot. A navigation band always
    takes its own renderer (both bands are the same application list); a
    primary/plain slot avoids repeating a renderer already placed, so a
    bare split does not put one view in both halves."""
    candidates = _candidates(role, renderers)
    if not candidates:
        return None
    if role == NAVIGATION:
        return candidates[0]
    for name in candidates:
        if name not in used:
            return name
    return candidates[0]


def _slot_params(renderer, renderers, applicable):
    """Navigation slots carry the applicable set explicitly, so the band
    lists what fits it rather than every advertised view."""
    if not applicable:
        return {}
    schema = ((renderers or {}).get(renderer) or {}).get("params") or {}
    return {"views": list(applicable)} if "views" in schema else {}


def build(payload, renderers=None):
    """A preset request -> the ``/layout`` payload ``parse_layout`` binds.

    Raises ValueError on an unknown preset, an unknown slot, or a preset
    whose slots no loaded view fits. Geometry errors, unknown renderers and
    capability fit are ``parse_layout``'s to report, from the payload this
    returns -- one authority on what applies."""
    if not isinstance(payload, dict):
        raise ValueError("a preset request must be an object")
    name = payload.get("preset")
    spec = PRESETS.get(name)
    if spec is None:
        raise ValueError("unknown layout preset %r (want one of: %s)"
                         % (name, ", ".join(PRESET_NAMES)))
    requested = payload.get("views") or {}
    if not isinstance(requested, dict):
        raise ValueError("'views' must be an object of slot -> renderer")
    roles = slot_roles(name)
    unknown = sorted(set(requested) - set(roles))
    if unknown:
        raise ValueError("preset %r has no slot %r (slots: %s)"
                         % (name, unknown[0], ", ".join(sorted(roles))))
    applicable = (caps.offered(renderers)
                  if spec.get("navigation") else [])
    regions, used = [], set()
    for slot, geometry in spec["regions"]:
        renderer = requested.get(slot)
        if renderer is not None:
            if not isinstance(renderer, str) or not renderer:
                raise ValueError("views[%r] must be a renderer name" % slot)
        else:
            renderer = _default(roles[slot], renderers, used)
        if renderer is None:
            raise ValueError("no loaded view fits the %s slot %r of preset "
                             "%r" % (roles[slot], slot, name))
        used.add(renderer)
        region = {"name": slot, "renderer": renderer}
        region.update(geometry)
        params = _slot_params(renderer, renderers, applicable)
        if params:
            region["params"] = params
        regions.append(region)
    return {"regions": regions}


def doc(renderers=None):
    """The read-only projection ``GET /layout/presets`` serves: every
    style, its slots, and the views each slot may be given."""
    out = []
    for name, spec in PRESETS.items():
        roles = slot_roles(name)
        slots = []
        for slot, geometry in spec["regions"]:
            role = roles[slot]
            slots.append({"name": slot, "role": role,
                          "geometry": dict(geometry),
                          "default": _default(role, renderers, ()),
                          "views": _candidates(role, renderers)})
        out.append({"name": name, "label": spec["label"],
                    "hint": spec["hint"], "primary": spec.get("primary"),
                    "slots": slots})
    return out
