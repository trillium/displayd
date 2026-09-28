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
``reload``, ``feed``, ``state``, ``snapshot``, ``renderers``, and the ``feedback_*``
family.

If displayd is unreachable every tool fails LOUDLY -- ``isError`` with
"display unreachable ..." -- and in particular ``feedback_record`` refuses
to store a frameless note silently: the note is NOT recorded and the error
says to retry once the panel is back.

Split across single-concept modules within the project's 250-line budget:
``mcp_client`` (HTTP client), ``mcp_schema`` (schema conversion),
``mcp_tools`` (tool catalog), ``mcp_result`` (result envelopes). Names
used by existing importers and tests are re-exported here.
"""

import json
import sys
import urllib.parse

from mcp_client import (DISPLAYD_URL, TIMEOUT, DisplayError,
                        DisplayUnreachable, api_get, api_post)
from mcp_result import err_text, ok_image, ok_text, unreachable
from mcp_tools import dynamic_tools, static_tools

SERVER_NAME = "displayd-mcp"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"


# ---- tool definitions ---------------------------------------------------

def fetch_renderers():
    """Live view list; raises DisplayUnreachable/DisplayError."""
    data = api_get("/renderers")
    return data.get("renderers", [])


def list_tools():
    tools = static_tools()
    try:
        tools.extend(dynamic_tools(fetch_renderers()))
    except (DisplayUnreachable, DisplayError):
        pass  # static tools stay; calls report the outage loudly
    return tools


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
        if name == "reload":
            body = {"sha": args.get("sha")}
            for key in ("highlights", "duration"):
                if args.get(key) is not None:
                    body[key] = args[key]
            return ok_text(api_post("/reload", body))
        if name == "reload_confirm":
            return ok_text(api_post("/reload/confirm", {"via": "tap"}))
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
