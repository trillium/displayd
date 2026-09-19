#!/usr/bin/env python3
"""displayd-mcp - an MCP server fronting the displayd (Jumbotron) HTTP API.

Stdlib only (no extra dependencies, matching displayd itself): it speaks
MCP's JSON-RPC over stdio (one message per line on stdin, responses on
stdout) and drives displayd through its existing HTTP API. Run it with::

    python3 mcp_server.py                      # DISPLAYD_URL defaults here
    DISPLAYD_URL=http://100.81.88.113:8980 python3 mcp_server.py

Configuration is by environment only -- displayd has no auth, so there is
no credential to read, hardcode, or print (acceptance criterion):

* ``DISPLAYD_URL`` -- base URL of the displayd HTTP API
  (default ``http://127.0.0.1:8980``).
* ``DISPLAYD_TIMEOUT`` -- per-request seconds (default ``10``).

Tool discovery is driven live from ``GET /renderers``: every ``tools/list``
re-reads the view list, so a new renderer file (or a new INPUTS entry)
appears as ``show_<view>`` / ``feed_<view>_<input>`` tools with no code
change here. The highest-value agent tools are ``show``, ``notify``,
``feed``, ``state``, ``snapshot``, ``renderers``, and the ``feedback_*``
family.

If displayd is unreachable every tool fails LOUDLY -- ``isError`` with
"display unreachable ..." -- and in particular ``feedback_record`` refuses
to store a frameless note silently: the note is NOT recorded and the error
says to retry once the panel is back.
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

SERVER_NAME = "displayd-mcp"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

DISPLAYD_URL = os.environ.get("DISPLAYD_URL", "http://127.0.0.1:8980").rstrip("/")
try:
    TIMEOUT = float(os.environ.get("DISPLAYD_TIMEOUT", "10"))
except ValueError:
    TIMEOUT = 10.0


# ---- displayd HTTP client ---------------------------------------------

class DisplayUnreachable(Exception):
    """The panel/daemon could not be reached at all (TCP refused, DNS,
    timeout). Distinct from an HTTP error *from* displayd."""


class DisplayError(Exception):
    """displayd answered with a non-2xx status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _request(method, path, body=None, raw=False):
    url = DISPLAYD_URL + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode() or "{}")
            message = detail.get("error", str(detail))
        except ValueError:
            message = "HTTP %d" % exc.code
        raise DisplayError(exc.code, message)
    except (urllib.error.URLError, ConnectionError, TimeoutError,
            OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise DisplayUnreachable("%s: %s" % (url, reason))
    if raw or "image/" in ctype:
        return payload
    if not payload:
        return {}
    try:
        return json.loads(payload.decode())
    except ValueError:
        raise DisplayError(500, "displayd returned non-JSON")


def api_get(path, raw=False):
    return _request("GET", path, raw=raw)


def api_post(path, body=None):
    return _request("POST", path, body=body if body is not None else {})


# ---- schema conversion -------------------------------------------------

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


# ---- tool definitions ---------------------------------------------------

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


def fetch_renderers():
    """Live view list; raises DisplayUnreachable/DisplayError."""
    data = api_get("/renderers")
    return data.get("renderers", [])


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


def list_tools():
    tools = static_tools()
    try:
        tools.extend(dynamic_tools(fetch_renderers()))
    except (DisplayUnreachable, DisplayError):
        pass  # static tools stay; calls report the outage loudly
    return tools


# ---- tool results --------------------------------------------------------

def ok_text(data):
    return {"content": [{"type": "text",
                         "text": json.dumps(data, indent=2, default=str)}]}


def ok_image(png_bytes):
    return {"content": [{"type": "image", "data": base64.b64encode(png_bytes).decode(),
                         "mimeType": "image/png"}]}


def err_text(message):
    return {"content": [{"type": "text", "text": str(message)}], "isError": True}


def unreachable(exc):
    return err_text("display unreachable at %s (%s). The panel state is "
                    "unknown; nothing was changed."
                    % (DISPLAYD_URL, exc))


# ---- dispatch --------------------------------------------------------------

def call_tool(name, args):
    args = args or {}
    try:
        if name == "health":
            return ok_text(api_get("/health"))
        if name == "state":
            return ok_text(api_get("/state"))
        if name == "renderers":
            return ok_text(api_get("/renderers"))
        if name == "snapshot":
            try:
                return ok_image(api_get("/snapshot", raw=True))
            except DisplayError as exc:
                if exc.status == 404:
                    return err_text("no frame yet: nothing has been drawn "
                                    "since the daemon started")
                raise
        if name == "show":
            return ok_text(api_post("/show", {"renderer": args.get("renderer"),
                                              "params": args.get("params", {})}))
        if name == "feed":
            return ok_text(api_post("/feed/%s/%s" % (args.get("view"),
                                                     args.get("input")),
                                    args.get("payload")))
        if name == "feed_status":
            return ok_text(api_get("/feed/%s/%s" % (args.get("view"),
                                                    args.get("input"))))
        if name == "notify":
            body = {"title": args.get("title")}
            for key in ("body", "severity", "duration", "color"):
                if args.get(key) is not None:
                    body[key] = args[key]
            return ok_text(api_post("/notify", body))
        if name == "policy_get":
            return ok_text(api_get("/policy"))
        if name == "policy_set":
            return ok_text(api_post("/policy", args.get("patch", {})))
        if name == "clear":
            return ok_text(api_post("/clear"))
        if name == "screen":
            power = args.get("power")
            if power == "on":
                return ok_text(api_post("/screen/on"))
            if power == "off":
                return ok_text(api_post("/screen/off"))
            return err_text("power must be 'on' or 'off'")
        if name == "feedback_record":
            body = {"view": args.get("view"), "rating": args.get("rating")}
            for key in ("categories", "notes", "params", "agent",
                        "include_frame"):
                if key in args and args[key] is not None:
                    body[key] = args[key]
            try:
                return ok_text(api_post("/feedback", body))
            except DisplayUnreachable as exc:
                return err_text("display unreachable at %s (%s). The frame "
                                "could not be captured, so the note was NOT "
                                "recorded -- retry once the panel is back, or "
                                "pass include_frame=false to record a "
                                "frameless note explicitly."
                                % (DISPLAYD_URL, exc))
        if name == "feedback_list":
            query = ""
            if args.get("view"):
                query += ("view=" + urllib.parse.quote(str(args["view"]), safe=""))
            if args.get("limit") is not None:
                query += ("&" if query else "") + "limit=%s" % args["limit"]
            return ok_text(api_get("/feedback" + ("?" + query if query else "")))
        if name == "feedback_get":
            return ok_text(api_get("/feedback/" + urllib.parse.quote(
                str(args.get("id")), safe="")))
        if name == "feedback_summary":
            return ok_text(api_get("/feedback/summary"))

        # dynamic tools: resolve the view/input against a fresh /renderers
        # so names are validated, not trusted from the tools/list snapshot.
        renderers = {r.get("name"): r for r in fetch_renderers()}
        for tool in dynamic_tools(list(renderers.values())):
            if tool["name"] != name:
                continue
            if "_input" in tool:
                payload = (dict(args) if tool["_inline"]
                           else args.get("payload"))
                return ok_text(api_post("/feed/%s/%s" % (tool["_view"],
                                                         tool["_input"]),
                                        payload))
            params = {k: v for k, v in args.items()}
            return ok_text(api_post("/show", {"renderer": tool["_view"],
                                              "params": params}))
        return err_text("unknown tool: %s" % name)
    except DisplayUnreachable as exc:
        return unreachable(exc)
    except DisplayError as exc:
        return err_text("displayd refused (HTTP %d): %s" % (exc.status,
                                                             exc.message))


# ---- JSON-RPC loop ----------------------------------------------------------

def handle_message(msg):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "invalid request"}}
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"protocolVersion": PROTOCOL_VERSION,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": SERVER_NAME,
                                          "version": SERVER_VERSION}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        public = []
        for tool in list_tools():
            public.append({k: v for k, v in tool.items()
                           if not k.startswith("_")})
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": public}}
    if method == "tools/call":
        result = call_tool(params.get("name"), params.get("arguments"))
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if msg_id is None:
        return None  # notification we do not handle: ack by silence
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": "method not found: %s" % method}}


def main():
    stdin, stdout = sys.stdin, sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"}}
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
            continue
        resp = handle_message(msg)
        if resp is None:
            continue
        stdout.write(json.dumps(resp) + "\n")
        stdout.flush()


if __name__ == "__main__":
    main()
