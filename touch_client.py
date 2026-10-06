"""The displayd HTTP client and its caller rule.

Single concept: the only place touch talks to the daemon -- the loopback
endpoint default, ``endpoint_allowed()`` (who/where may be called), the
``/touch/announce`` heartbeat POST, the ``GET /renderers`` view fetch, the
``--check-views`` cross-check, the reload-dismissal path, and
``DisplaydClient`` (dispatch + state). Nothing here knows about regions,
taps, or config files.
"""

import ipaddress
import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import touch_audit

from touch_resolve import resolve_action

LOG = logging.getLogger("displayd-touch")

DEFAULT_ENDPOINT = "http://127.0.0.1:8980"

# Tailnet carrier-grade NAT range: 100.64.0.0/10. An endpoint whose host
# is inside it is a tailnet address; anything else non-loopback is not.
TAILNET_CGNAT = "100.64.0.0/10"


def endpoint_allowed(url):
    """Caller rule: loopback or tailnet only, never the open internet.

    Mirrors origin.ts (WHO may call): parlay allows no-Origin
    server-to-server callers plus loopback / private-LAN / allow-listed
    origins and denies "null"; touch is stricter because it has no LAN
    caller -- only 127.0.0.0/8, ::1, localhost, or 100.64.0.0/10. Closed
    by default: LAN literals, public IPs, non-local hostnames (including
    MagicDNS -- use the tailnet IP literal), and non-http(s) schemes all
    fail. Unparseable input fails rather than defaulting open.
    """
    try:
        parts = urlsplit(url)
    except Exception:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname or ""
    host = host.strip().strip("[]").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # non-local hostname: closed (see residue note)
    if addr.is_loopback:
        return True
    try:
        return addr in ipaddress.ip_network(TAILNET_CGNAT)
    except ValueError:
        return False
# Dismissal path for the reload confirmation (displayd POST /touch/tap):
# sent before hit-testing on every valid tap, so a tap anywhere returns an
# active reload view through its normal return path. Dismissing anything
# but an active reload is a server-side no-op, and a failed dismissal must
# never block the configured region action that follows.
TAP_DISMISS_PATH = "/touch/tap"


def post_announce(config, timeout=5.0):
    """POST the effective region set to displayd (touch heartbeat).

    Returns the daemon's ack dict. Raises RuntimeError on failure: the
    startup caller treats it best-effort (taps must serve even when
    displayd is down); --announce surfaces it as exit 2."""
    payload = touch_audit.announce_payload(config)
    url = (config.get("endpoint") or DEFAULT_ENDPOINT).rstrip(
        "/") + "/touch/announce"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")
                              or "{}")
    except Exception as exc:
        raise RuntimeError("POST %s failed: %s" % (url, exc)) from exc


def fetch_renderers(base_url, timeout=5.0):
    """GET /renderers view names from a live daemon. Pure HTTP read;
    raises RuntimeError when the daemon is unreachable or answers garbage."""
    url = base_url.rstrip("/") + "/renderers"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise RuntimeError("GET %s failed: %s" % (url, exc)) from exc
    try:
        return sorted(r["name"] for r in doc.get("renderers") or []
                      if isinstance(r, dict) and r.get("name"))
    except Exception as exc:
        raise RuntimeError("GET %s answered garbage: %s"
                           % (url, exc)) from exc


def check_views(config, fetch=None):
    """Cross-check every configured selection against the daemon's
    advertised renderer set: each select_view tile's view plus the
    tap_options fallback renderer. Returns a report dict with "ok".
    `fetch` is injectable (base_url -> [names]) for tests."""
    fetch = fetch or fetch_renderers
    advertised = set(fetch(config["endpoint"]))
    scoped_views = config.get("view_regions") or {}
    wanted = []
    regions = list(config.get("regions") or [])
    for scoped in scoped_views.values():
        regions.extend(scoped or [])
    for region in regions:
        action = region.get("action") or {}
        if (action.get("name") == "select_view"
                and action.get("view")):
            wanted.append((region.get("id"), action["view"]))
    tap = config.get("tap_options") or {}
    if tap.get("enabled") and tap.get("renderer"):
        wanted.append(("(tap_options)", tap["renderer"]))
    unknown = sorted({view for _, view in wanted
                      if view not in advertised})
    return {"endpoint": config["endpoint"],
            "advertised": sorted(advertised),
            "selected": [{"region": rid, "view": view}
                           for rid, view in wanted],
            "unknown": unknown,
            "ok": not unknown}


class DisplaydClient:
    """Minimal stdlib HTTP client for the displayd API."""

    def __init__(self, base_url=DEFAULT_ENDPOINT, timeout=5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def post(self, path, body):
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(payload or "{}")
                except ValueError:
                    return resp.status, {"raw": payload}
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            raise RuntimeError("displayd %s -> HTTP %s: %s"
                               % (path, exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("displayd %s unreachable at %s: %s"
                               % (path, self.base_url, exc.reason)) from exc

    def dispatch(self, action, dry_run=False, panel=None):
        """Validate + (unless dry_run) POST an action dict. Returns a
        summary dict describing what was (or would be) done.

        Policy application point (index.ts shape): unknown actions and
        disallowed endpoints are denied silently -- an error summary, no
        exception, no byte on the wire -- so a bad config can neither
        crash the service loop nor reach an unexpected caller. `panel`
        is the (width, height) pair bounding coordinate actions (the
        tap point for the coordinate actions); other actions ignore it."""
        name = action.get("name") if isinstance(action, dict) else None
        resolved = resolve_action(action, panel=panel)
        if resolved is None:
            return {"action": name, "dry_run": bool(dry_run),
                    "error": "refusing unknown action: %r" % (name,)}
        method, path, body = resolved
        summary = {"action": name, "method": method,
                   "path": path, "body": body, "dry_run": bool(dry_run)}
        if not endpoint_allowed(self.base_url):
            LOG.warning("touch dispatch denied: endpoint %r is not "
                        "loopback or tailnet (closed caller rule)",
                        self.base_url)
            summary["error"] = ("refusing endpoint %r: not loopback or "
                                  "tailnet" % (self.base_url,))
            return summary
        if dry_run:
            return summary
        status, resp = self.post(path, body)
        summary["status"] = status
        summary["response"] = resp
        return summary

    def state(self, timeout=None):
        """GET /state -> daemon status dict (raises RuntimeError). The
        touch service reads only the showing renderer; short timeout so a
        sick daemon never stalls the tap path."""
        url = self.base_url.rstrip("/") + "/state"
        try:
            with urllib.request.urlopen(
                    url, timeout=self.timeout if timeout is None
                    else timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            raise RuntimeError("GET %s failed: %s" % (url, exc)) from exc

    def tap_dismiss(self, dry_run=False):
        """Dismiss an active reload confirmation (POST /touch/tap).

        Harmless server-side unless a reload transient is showing; raises
        like post() on transport failure so the caller can log-and-continue
        without disturbing the region action that follows."""
        summary = {"action": "tap_dismiss", "method": "POST",
                   "path": TAP_DISMISS_PATH, "body": {},
                   "dry_run": bool(dry_run)}
        if dry_run:
            return summary
        status, resp = self.post(TAP_DISMISS_PATH, {})
        summary["status"] = status
        summary["response"] = resp
        return summary
