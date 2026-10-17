"""Renderer implementations behind the surface API.

Single concept: what *draws* content once the hub has resolved a target.
The registry and hub know only this small interface -- a renderer
implementation is looked up by the surface's ``renderer.name``:

``validate(content)``
    refuse a malformed payload for this implementation (``ValueError``).
``render(surface, region, content)``
    put it on the surface; returns a dict that is merged into the delivery
    result (``rendered``: true when displayd itself drew it).

Two implementations ship:

``RelayRenderer`` (the fallback for any renderer name displayd does not
run itself)
    a Sidecar draws for itself, so delivery is just "store it in the
    region's slot": the Sidecar pulls it with ``GET /surfaces/<id>/content``.
``HtmlSurfaceRenderer`` (``displayd-html``)
    DisplayD's on-device litehtml renderer. Content is ``{"type":
    "displayd-html", "template": "status.html", "vars": {...}}`` -- a named
    trusted template plus escaped variables, never markup -- drawn into the
    panel region by ``apply(region_name, params)`` (the daemon's own region
    machinery), so one region updating never disturbs the others.
"""

import re

TEMPLATE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.html$")
HTML_RENDERER = "displayd-html"
HTML_CONTENT_TYPE = "displayd-html"


class SurfaceRenderer:
    """The interface; a subclass names itself and its content types."""

    name = ""
    content_types = ()

    def validate(self, content):
        return None

    def render(self, surface, region, content):
        return {"rendered": False}


class RelayRenderer(SurfaceRenderer):
    name = "relay"

    def render(self, surface, region, content):
        return {"rendered": False, "delivery": "pull"}


class HtmlSurfaceRenderer(SurfaceRenderer):
    name = HTML_RENDERER
    content_types = (HTML_CONTENT_TYPE,)

    def __init__(self, apply):
        self._apply = apply  # (region_name, html params) -> None

    @staticmethod
    def params(content):
        params = {}
        if "template" in content:
            params["template"] = content["template"]
        if "vars" in content:
            params["vars"] = content["vars"]
        return params

    def validate(self, content):
        template = content.get("template")
        if template is not None and (not isinstance(template, str)
                                     or not TEMPLATE_RE.match(template)):
            raise ValueError("template: expected a template file name like "
                             "'status.html'")
        variables = content.get("vars")
        if variables is not None:
            if not isinstance(variables, dict):
                raise ValueError("vars: expected an object")
            for key, value in variables.items():
                if not isinstance(value, (str, int, float, bool)):
                    raise ValueError("vars.%s: expected a string or number" % key)

    def render(self, surface, region, content):
        self._apply(region["name"], self.params(content))
        return {"rendered": True, "delivery": "push"}
