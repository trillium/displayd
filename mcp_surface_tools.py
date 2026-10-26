"""mcp_surface_tools - MCP tools for surfaces, streams, columns and touch.

Single concept: the agent-facing tool surface of the registered-surfaces API
(``docs/SURFACES.md``). One table row per tool: its schema and a function from
the tool's arguments to ``(method, path, body)``. The routes are the HTTP API's
own, so this module adds no behaviour and no second path: ``mcp_server`` hands
``call`` its own ``api_get`` / ``api_post`` / ``api_delete``.

Long polls (``wait``) are clamped below the client timeout so a held-open read
never looks like an unreachable panel.
"""

from urllib.parse import quote, urlencode

from mcp_client import TIMEOUT

MAX_WAIT = 25.0
_STR, _OBJ, _NUM = {"type": "string"}, {"type": "object"}, {"type": "number"}


def _seg(value):
    if not isinstance(value, str) or not value:
        raise ValueError("expected a non-empty id")
    return quote(value, safe="")


def _wait(args):
    return max(0.0, min(float(args.get("wait") or 0), MAX_WAIT, TIMEOUT - 1.0))


def _poll(path, args):
    query = {"since": args.get("since") or 0}
    if _wait(args):
        query["wait"] = _wait(args)
    return "GET", "%s?%s" % (path, urlencode(query)), None


def _discover(args):
    query = {k: v for k, v in args.items() if k != "labels" and v is not None}
    for key, value in (args.get("labels") or {}).items():
        query["label." + key] = value
    return "GET", "/surfaces" + ("?" + urlencode(query) if query else ""), None


def _send(args):
    body = {"target": args.get("target"), "content": args.get("content")}
    return "POST", "/surfaces/content", body


def _declare(args):
    keys = ("id", "name", "event_types", "views", "actions")
    return "POST", "/event-streams", {k: args[k] for k in keys if k in args}


def _action(args):
    body = {k: args[k] for k in ("action", "event_seq", "value") if k in args}
    return "POST", "/surfaces/%s/columns/%s/action" % (_seg(args.get("id")),
                                                        _seg(args.get("column"))), body


def _publish(args):
    return ("POST", "/event-streams/%s/events" % _seg(args.get("id")),
            {"type": args.get("type"), "data": args.get("data")})


def _sid(path, method="GET", body_key=None):
    def route(args):
        body = args.get(body_key) if body_key else None
        if body_key and not isinstance(body, dict):
            raise ValueError("%s: expected an object" % body_key)
        return method, path % _seg(args.get("id")), body
    return route


def _tool(name, desc, props, required, route):
    return {"name": name, "description": desc, "_route": route,
            "inputSchema": dict({"type": "object", "properties": props},
                                **({"required": required} if required else {}))}


_ID = {"id": dict(_STR, description="surface id")}
_SID = {"id": dict(_STR, description="stream id")}
_POLL = {"since": dict(_NUM, description="only items after this seq"),
         "wait": dict(_NUM, description="long-poll seconds (clamped to the client timeout)")}

TOOLS = [
    _tool("surfaces_list",
          "Discover live surfaces, filtered (kind, renderer, content_type, touch, "
          "min_width, min_height, region, labels).",
          {"kind": _STR, "renderer": _STR, "content_type": _STR, "touch": _STR,
           "min_width": _NUM, "min_height": _NUM, "region": _STR, "labels": _OBJ},
          None, _discover),
    _tool("surface_get", "One surface: viewport, regions, touch, panel block.",
          _ID, ["id"], _sid("/surfaces/%s")),
    _tool("surface_register", "Register (upsert) a surface from a full spec; give regions as rects, "
          "or a 'layout' (stack/grid/rect) displayd partitions inside the viewport.",
          {"spec": _OBJ}, ["spec"],
          lambda a: ("POST", "/surfaces", a.get("spec"))),
    _tool("surface_update", "Merge a partial spec into a surface (the built-in "
          "panel only accepts a 'panel' patch).", dict(_ID, patch=_OBJ), ["id", "patch"],
          _sid("/surfaces/%s", "POST", "patch")),
    _tool("surface_heartbeat", "Extend a registered surface's lease.",
          _ID, ["id"], _sid("/surfaces/%s/heartbeat", "POST")),
    _tool("surface_deregister", "Remove a registered surface.",
          _ID, ["id"], _sid("/surfaces/%s", "DELETE")),
    _tool("surface_send",
          "Send content to a target: a surface id, 'id/region', or a select query.",
          {"target": {"description": "surface id, 'id/region', or select object"},
           "content": _OBJ}, ["target", "content"], _send),
    _tool("surface_events", "Read a surface's reported touch events.",
          dict(_ID, **_POLL), ["id"],
          lambda a: _poll("/surfaces/%s/events" % _seg(a.get("id")), a)),
    _tool("surface_touch_report", "Report one touch interaction on a registered "
          "surface (what a Sidecar does).", dict(_ID, event=_OBJ), ["id", "event"],
          _sid("/surfaces/%s/touch", "POST", "event")),
    _tool("surface_columns", "A surface's columns with what their streams offer.",
          _ID, ["id"], _sid("/surfaces/%s/columns")),
    _tool("surface_columns_set", "Replace a surface's columns "
          "([{id, stream, view, region, options?}]); on the panel they are drawn.",
          dict(_ID, columns={"type": "array", "items": _OBJ}), ["id", "columns"],
          lambda a: ("POST", "/surfaces/%s/columns" % _seg(a.get("id")),
                     {"columns": a.get("columns")})),
    _tool("column_action", "A viewer's follow-on action on a column's stream.",
          dict(_ID, column=_STR, action=_STR, event_seq=_NUM, value={}),
          ["id", "column", "action"], _action),
    _tool("streams_list", "Declared event streams.", {}, None,
          lambda a: ("GET", "/event-streams", None)),
    _tool("stream_declare", "Declare (upsert) an event stream.",
          {"id": _STR, "name": _STR, "event_types": {"type": "array", "items": _STR},
           "views": {"type": "array", "items": _STR},
           "actions": {"type": "array", "items": _OBJ}}, ["id"], _declare),
    _tool("stream_get", "One stream's declaration and seq counters.",
          _SID, ["id"], _sid("/event-streams/%s")),
    _tool("stream_remove", "Remove a stream.", _SID, ["id"],
          _sid("/event-streams/%s", "DELETE")),
    _tool("stream_publish", "Publish one event to a stream.",
          dict(_SID, type=_STR, data={}), ["id", "type"], _publish),
    _tool("stream_events", "Read a stream's events after `since`.",
          dict(_SID, **_POLL), ["id"],
          lambda a: _poll("/event-streams/%s/events" % _seg(a.get("id")), a)),
    _tool("stream_actions", "Read the follow-on actions viewers took.",
          dict(_SID, **_POLL), ["id"],
          lambda a: _poll("/event-streams/%s/actions" % _seg(a.get("id")), a)),
]
BY_NAME = {t["name"]: t for t in TOOLS}


def tools():
    """The catalog entries (schemas only; no route callables)."""
    return [{k: v for k, v in t.items() if not k.startswith("_")} for t in TOOLS]


def call(name, args, get, post, delete):
    """Run a surface tool; ``(True, result)`` or ``(False, None)`` when
    ``name`` is not one of these. ``ValueError`` means bad arguments."""
    tool = BY_NAME.get(name)
    if tool is None:
        return False, None
    method, path, body = tool["_route"](args)
    if method == "GET":
        return True, get(path)
    if method == "DELETE":
        return True, delete(path)
    return True, post(path, body)
