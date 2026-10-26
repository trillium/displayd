"""Registered display surfaces: the vocabulary and the one validator.

Single concept: what a *surface* is, independent of any renderer. A surface
is anything that can show content -- the panel itself, a Sidecar browser tab,
an iPad -- and describes itself with four things:

``viewport``
    ``width``/``height`` in surface pixels, ``scale`` (device pixel ratio) and
    optional ``insets`` (notch, bezel, dock). The derived ``usable`` rect is
    what content may actually occupy; every region lives inside it.
``renderer``
    ``name`` (the implementation that draws on the surface), optional
    ``version``, and ``content_types``: the content ``type`` strings it
    accepts. displayd never interprets these beyond matching them.
``touch``
    ``supported`` plus the ``interactions`` it can report (closed vocabulary
    ``TOUCH_INTERACTIONS``) and ``max_points``. No touch means no interactions.
``regions`` / ``layout``
    named rects inside the usable viewport that consumers can target: given,
    or derived from a ``layout`` (``surface_layout``); with neither, one
    ``full`` region covers the usable viewport.

``normalize`` is the only place a spec is judged: a bad one raises
``ValueError`` naming the field. Its output is itself a valid input
(idempotent), so an update is "merge a patch, validate again".
"""

import re
import surface_layout

TOUCH_INTERACTIONS = ("tap", "double_tap", "long_press", "swipe", "drag",
                      "pinch", "multi_touch")
MULTI_POINT = ("pinch", "multi_touch")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")
RESERVED_IDS = ("content",)  # POST /surfaces/content is the targeted send
DEFAULT_KIND = "sidecar"
DEFAULT_REGION = "full"
DEFAULT_TTL = 30.0
MIN_TTL, MAX_TTL = 1.0, 3600.0
MAX_DIM, MAX_REGIONS, MAX_CONTENT_TYPES, MAX_LABELS, MAX_POINTS = 16384, 32, 16, 16, 10
INSET_SIDES = ("top", "right", "bottom", "left")


class SurfaceError(Exception):
    """A refusal with an HTTP status (404/409/410/415); ValueError is 400."""

    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status


def _int(value, where, low, high):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s: expected an integer" % where)
    if not low <= value <= high:
        raise ValueError("%s: %d outside %d..%d" % (where, value, low, high))
    return value


def _token(value, where, pattern=TOKEN_RE):
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError("%s: expected a short lowercase token, got %r"
                         % (where, value))
    return value


def _dict(value, where):
    if not isinstance(value, dict):
        raise ValueError("%s: expected an object" % where)
    return value


def _content_types(value, where):
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("%s: expected a non-empty list of content types" % where)
    if len(value) > MAX_CONTENT_TYPES:
        raise ValueError("%s: at most %d content types" % (where, MAX_CONTENT_TYPES))
    out = []
    for item in value:
        item = _token(item, where)
        if item not in out:
            out.append(item)
    return out


def _renderer(raw):
    raw = _dict(raw, "renderer")
    version = raw.get("version")
    if version is not None and (not isinstance(version, str) or len(version) > 32):
        raise ValueError("renderer.version: expected a string of at most 32 chars")
    return {"name": _token(raw.get("name"), "renderer.name"),
            "version": version,
            "content_types": _content_types(raw.get("content_types"),
                                            "renderer.content_types")}


def _viewport(raw):
    raw = _dict(raw, "viewport")
    width = _int(raw.get("width"), "viewport.width", 1, MAX_DIM)
    height = _int(raw.get("height"), "viewport.height", 1, MAX_DIM)
    scale = raw.get("scale", 1.0)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) \
            or not 0 < scale <= 8:
        raise ValueError("viewport.scale: expected a number in (0, 8]")
    insets_in = _dict(raw.get("insets") or {}, "viewport.insets")
    insets = {side: _int(insets_in.get(side, 0), "viewport.insets.%s" % side,
                         0, MAX_DIM) for side in INSET_SIDES}
    usable = {"x": insets["left"], "y": insets["top"],
              "w": width - insets["left"] - insets["right"],
              "h": height - insets["top"] - insets["bottom"]}
    if usable["w"] < 1 or usable["h"] < 1:
        raise ValueError("viewport.insets: leave no usable area")
    orientation = ("square" if width == height
                   else "landscape" if width > height else "portrait")
    return {"width": width, "height": height, "scale": float(scale),
            "orientation": orientation, "insets": insets, "usable": usable}


def _touch(raw):
    raw = _dict(raw if raw is not None else {}, "touch")
    supported = raw.get("supported", False)
    if not isinstance(supported, bool):
        raise ValueError("touch.supported: expected a boolean")
    listed = raw.get("interactions")
    if not supported:
        if listed:
            raise ValueError("touch.interactions: listed but touch.supported is false")
        return {"supported": False, "interactions": [], "max_points": 0}
    listed = ["tap"] if listed is None else listed
    if not isinstance(listed, (list, tuple)) or not listed:
        raise ValueError("touch.interactions: expected a non-empty list")
    for item in listed:
        if item not in TOUCH_INTERACTIONS:
            raise ValueError("touch.interactions: unknown %r (one of %s)"
                             % (item, ", ".join(TOUCH_INTERACTIONS)))
    points = _int(raw.get("max_points", 1), "touch.max_points", 1, MAX_POINTS)
    if points < 2 and any(item in MULTI_POINT for item in listed):
        raise ValueError("touch.max_points: pinch/multi_touch need at least 2")
    return {"supported": True, "max_points": points,
            "interactions": [i for i in TOUCH_INTERACTIONS if i in listed]}


def _rect(raw, usable, where):
    raw = _dict(raw, where)
    rect = {key: _int(raw.get(key), "%s.%s" % (where, key), 0 if key in "xy" else 1,
                      MAX_DIM) for key in ("x", "y", "w", "h")}
    if (rect["x"] < usable["x"] or rect["y"] < usable["y"]
            or rect["x"] + rect["w"] > usable["x"] + usable["w"]
            or rect["y"] + rect["h"] > usable["y"] + usable["h"]):
        raise ValueError("%s: outside the usable viewport" % where)
    return rect


def default_regions(usable, renderer, touch):
    return [{"name": DEFAULT_REGION, "rect": dict(usable),
             "content_types": list(renderer["content_types"]),
             "touch": touch["supported"]}]


def _regions(raw, field, usable, renderer, touch):
    if raw is None:
        return default_regions(usable, renderer, touch)
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("%s: expected a non-empty list" % field)
    if len(raw) > MAX_REGIONS:
        raise ValueError("%s: at most %d" % (field, MAX_REGIONS))
    out, seen = [], set()
    for i, item in enumerate(raw):
        where = "%s[%d]" % (field, i)
        item = _dict(item, where)
        name = _token(item.get("name"), where + ".name")
        if name in seen:
            raise ValueError("%s: duplicate region name %r" % (where, name))
        seen.add(name)
        types = item.get("content_types")
        types = list(renderer["content_types"]) if types is None else \
            _content_types(types, where + ".content_types")
        stray = [t for t in types if t not in renderer["content_types"]]
        if stray:
            raise ValueError("%s.content_types: %s not accepted by the renderer"
                             % (where, ", ".join(stray)))
        wants_touch = item.get("touch", touch["supported"])
        if not isinstance(wants_touch, bool):
            raise ValueError("%s.touch: expected a boolean" % where)
        if wants_touch and not touch["supported"]:
            raise ValueError("%s.touch: the surface has no touch" % where)
        out.append({"name": name, "rect": _rect(item.get("rect"), usable, where + ".rect"),
                    "content_types": types, "touch": wants_touch})
    return out


def _labels(raw):
    raw = _dict(raw or {}, "labels")
    if len(raw) > MAX_LABELS:
        raise ValueError("labels: at most %d" % MAX_LABELS)
    for key, value in raw.items():
        _token(key, "labels key")
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("labels.%s: expected a string of at most 64 chars" % key)
    return dict(raw)


def _text(value, where, default, limit=80):
    value = default if value is None else value
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("%s: expected a non-empty string of at most %d chars"
                         % (where, limit))
    return value


def default_region_name(regions):
    """The region a send lands in when none is named: ``full`` if declared,
    else the only one, else None (the sender must say which)."""
    names = [r["name"] for r in regions]
    return DEFAULT_REGION if DEFAULT_REGION in names else (
        names[0] if len(names) == 1 else None)


def is_default_regions(spec):
    """True when ``spec['regions']`` is what normalize derived -- the default
    or a stored ``layout`` -- so a viewport/renderer/touch change re-derives."""
    return spec.get("layout") is not None or spec.get("regions") == default_regions(
        spec["viewport"]["usable"], spec["renderer"], spec["touch"])


def normalize(spec, builtin=False, default_ttl=DEFAULT_TTL):
    """One registration spec -> one stored shape, or ``ValueError``."""
    spec = _dict(spec, "surface")
    sid = _token(spec.get("id"), "id", ID_RE)
    if sid in RESERVED_IDS:
        raise ValueError("id: %r is reserved" % sid)
    renderer = _renderer(spec.get("renderer"))
    viewport = _viewport(spec.get("viewport"))
    touch = _touch(spec.get("touch"))
    regions = _regions(*surface_layout.source(spec, viewport["usable"], MAX_REGIONS),
                       viewport["usable"], renderer, touch)
    ttl = None
    if not builtin:
        ttl = spec.get("ttl_seconds", default_ttl)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) \
                or not MIN_TTL <= ttl <= MAX_TTL:
            raise ValueError("ttl_seconds: expected %g..%g" % (MIN_TTL, MAX_TTL))
    return {"id": sid, "name": _text(spec.get("name"), "name", sid),
            "kind": _token(spec.get("kind", DEFAULT_KIND), "kind"),
            "builtin": builtin, "renderer": renderer, "viewport": viewport,
            "touch": touch, "regions": regions,
            "default_region": default_region_name(regions),
            "labels": _labels(spec.get("labels")),
            "ttl_seconds": None if ttl is None else float(ttl), **surface_layout.stored(spec)}
