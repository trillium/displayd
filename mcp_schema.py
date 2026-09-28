"""mcp_schema - displayd param specs to JSON Schema.

Single concept: translate one displayd param/input spec (``type``,
``help``, ``properties``, ``required``, ``items``) into a JSON Schema
fragment for the MCP ``inputSchema`` surface, plus the tool-name
sanitiser. Pure functions, no I/O.
"""

import re

_TYPE_MAP = {"string": "string", "integer": "integer", "number": "number",
             "boolean": "boolean", "object": "object", "array": "array"}


def to_json_schema(spec):
    """One displayd param/input spec -> JSON Schema fragment."""
    spec = spec or {}
    out = {"type": _TYPE_MAP.get(spec.get("type", "string"), "string")}
    if spec.get("help"):
        out["description"] = spec["help"]
    if out["type"] == "object":
        props = {}
        for key, sub in (spec.get("properties") or {}).items():
            props[key] = to_json_schema(sub)
        if props:
            out["properties"] = props
        if spec.get("required"):
            out["required"] = list(spec["required"])
    if out["type"] == "array" and "items" in spec:
        out["items"] = to_json_schema(spec["items"])
    return out


def _sanitize(name):
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)
