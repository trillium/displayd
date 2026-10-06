"""The preview feed's wire documents and POSTs.

Single concept: one preview set turned into the /feed/macbook/preview
document and posted -- with "nothing to show" reported as None so the panel
keeps its boxes + PREVIEW OFF instead of ingesting an empty set.
"""

import json
import time
import urllib.request


def preview_doc(frames, now=None):
    """Frame list -> one /feed/macbook/preview document.

    None when there is nothing to show (no POST: the panel keeps
    boxes + PREVIEW OFF rather than ingesting an empty set)."""
    try:
        if not frames:
            return None
        return {"ts": float(now if now is not None else time.time()),
                "frames": list(frames)}
    except (TypeError, ValueError):
        return None


def post_preview(displayd_base, doc, timeout=5.0):
    """POST one preview document to the panel feed; True on success."""
    url = displayd_base.rstrip("/") + "/feed/macbook/preview"
    import json
    req = urllib.request.Request(url, data=json.dumps(doc).encode("utf-8"),
                                 headers={"Content-Type":
                                          "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1024)
    except Exception:
        return False
    return True
