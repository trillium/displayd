"""mcp_tools - the MCP tool catalog for displayd.

Single concept: which tools exist. The static core table (hand-written,
stable) plus the dynamic ``show_<view>`` / ``feed_<view>_<input>`` tools
derived from a live ``/renderers`` listing, so a new renderer file (or a
new INPUTS entry) appears with no code change here. Pure data shaping;
the HTTP fetch itself stays with the caller.
"""

from mcp_schema import _sanitize, to_json_schema


def static_tools():
    obj = {"type": "object"}
    return [
        {"name": "health", "description": "displayd liveness probe.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "state",
         "description": ("What is on the panel now: current view, screen "
                         "power, feed health, switch timing."),
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "renderers",
         "description": ("Self-describing view list: params, inputs, and "
                         "required flags. Drives tool discovery."),
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "snapshot",
         "description": ("PNG of the last presented frame, returned as an "
                         "image content block. 404 when nothing drawn yet."),
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "show",
         "description": "Switch the panel to a view with params.",
         "inputSchema": {"type": "object",
                          "properties": {
                              "renderer": {"type": "string",
                                           "description": "view name from renderers"},
                              "params": {"type": "object",
                                         "description": "view params"}},
                          "required": ["renderer"]}},
        {"name": "feed",
         "description": "Push a JSON payload into a running view's input.",
         "inputSchema": {"type": "object",
                          "properties": {
                              "view": {"type": "string"},
                              "input": {"type": "string"},
                              "payload": {"description": "payload (validated by displayd)"}},
                          "required": ["view", "input", "payload"]}},
        {"name": "feed_status",
         "description": "One feed's health (cold/warm/stale) and latest value.",
         "inputSchema": {"type": "object",
                          "properties": {"view": {"type": "string"},
                                         "input": {"type": "string"}},
                          "required": ["view", "input"]}},
        {"name": "notify",
         "description": "Transient notification card, then auto-return.",
         "inputSchema": {"type": "object",
                          "properties": {
                              "title": {"type": "string"},
                              "body": {"type": "string"},
                              "severity": {"type": "string",
                                           "enum": ["info", "warn", "critical"]},
                              "duration": {"type": "number"},
                              "color": {"type": "string"}},
                          "required": ["title"]}},
        {"name": "reload",
         "description": ("Transient reload confirmation (RELOADED + SHA + "
                           "scan-confirm QR, plus optional commit-message "
                           "highlights), then auto-return. Answers "
                           "relay_url when the scanning phone can reach it."),
         "inputSchema": {"type": "object",
                          "properties": {
                              "sha": {"type": "string",
                                        "description": "full 40-character deployed commit SHA"},
                              "highlights": {"type": "string",
                                               "description": "bounded commit-message summary; sanitised server-side, drawn as text only"},
                              "duration": {"type": "number"}},
                          "required": ["sha"]}},
        {"name": "reload_confirm",
         "description": ("Confirm the showing reload view early (tap "
                           "path): the panel returns at once. Misses with "
                           "an error when no reload is showing."),
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "policy_get", "description": "Policy config + activity clock.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "policy_set",
         "description": "Update policy (idle / chat_attention / notifications).",
         "inputSchema": {"type": "object",
                          "properties": {"patch": {"type": "object"}},
                          "required": ["patch"]}},
        {"name": "clear", "description": "Blank the panel to black.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "screen",
         "description": "Panel power on/off.",
         "inputSchema": {"type": "object",
                          "properties": {"power": {"type": "string",
                                                   "enum": ["on", "off"]}},
                          "required": ["power"]}},
        {"name": "feedback_record",
         "description": ("Record a judgement about a display: captures a "
                         "/snapshot at feedback time and links the frame, so "
                         "a later reader sees what was judged. Refuses "
                         "rather than storing silently when the panel is "
                         "unreachable."),
         "inputSchema": {"type": "object",
                          "properties": {
                              "view": {"type": "string",
                                       "description": "renderer the note concerns"},
                              "rating": {"type": "integer",
                                         "description": "1-5",
                                         "minimum": 1, "maximum": 5},
                              "categories": {"type": "array",
                                             "items": {"type": "string",
                                                       "enum": ["readability", "layout",
                                                                "color", "content",
                                                                "timing", "size", "other"]}},
                              "notes": {"type": "string"},
                              "params": {"type": "object",
                                         "description": "params in play when judged"},
                              "agent": {"type": "string"},
                              "include_frame": {"type": "boolean",
                                                "description": "capture /snapshot (default true); "
                                                               "set false only to record while "
                                                               "the panel is known-unreachable"}},
                          "required": ["view", "rating"]}},
        {"name": "feedback_list",
         "description": "Accumulated feedback, newest first, filterable by view.",
         "inputSchema": {"type": "object",
                          "properties": {"view": {"type": "string"},
                                         "limit": {"type": "integer"}},
                          }},
        {"name": "feedback_get",
         "description": "One feedback entry by id.",
         "inputSchema": {"type": "object",
                          "properties": {"id": {"type": "string"}},
                          "required": ["id"]}},
        {"name": "feedback_summary",
         "description": ("Patterns across entries: per-view counts, mean "
                         "rating, category histograms."),
         "inputSchema": {"type": "object", "properties": {}}},
    ]


def dynamic_tools(renderers):
    """show_<view> + feed_<view>_<input> tools derived from /renderers."""
    tools = []
    for renderer in renderers:
        name = renderer.get("name", "?")
        if renderer.get("broken"):
            continue
        safe = _sanitize(name)
        params = renderer.get("params") or {}
        props = {key: to_json_schema(spec) for key, spec in params.items()}
        required = [key for key, spec in params.items()
                    if isinstance(spec, dict) and spec.get("required")]
        schema = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        desc = renderer.get("description") or ""
        tools.append({"name": "show_%s" % safe,
                      "description": ("Show the %r view. %s"
                                      % (name, desc)).strip(),
                      "_view": name,
                      "inputSchema": schema})
        for input_name, spec in (renderer.get("inputs") or {}).items():
            spec = spec or {}
            if spec.get("type") == "object" and spec.get("properties"):
                props = {key: to_json_schema(sub)
                         for key, sub in spec["properties"].items()}
                schema = {"type": "object", "properties": props}
                if spec.get("required"):
                    schema["required"] = list(spec["required"])
                inline = True
            else:
                schema = {"type": "object",
                          "properties": {"payload": to_json_schema(spec)},
                          "required": ["payload"]}
                inline = False
            tools.append({"name": "feed_%s_%s" % (safe, _sanitize(input_name)),
                          "description": ("Feed the %r input of view %r."
                                          % (input_name, name)),
                          "_view": name, "_input": input_name,
                          "_inline": inline,
                          "inputSchema": schema})
    return tools
