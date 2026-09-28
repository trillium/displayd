"""mcp_result - MCP tool-result envelopes.

Single concept: wrap a dispatch outcome as an MCP ``tools/call`` result --
text JSON, PNG image block, or a loud ``isError`` failure. The
``unreachable`` wording is the loud-failure contract: the panel state is
unknown and nothing was changed.
"""

import base64
import json

from mcp_client import DISPLAYD_URL


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
