"""Deploy stamp and the reload confirmation's QR relay.

Single concept: delivery proof and its scan target -- the DEPLOYED stamp
written host-side by deploy.sh (read by GET /deploy and the control page), and
the one-time ``/r/<token>`` relay URL the reload QR encodes. The relay base URL
is derived here from the live bind, so a caller can never talk the daemon into
encoding an arbitrary QR target.
"""

import ipaddress
import json
import os
import re

from daemon_config import BIND, PORT

# Delivery stamp (written by deploy.sh, read by GET /deploy and the
# control page): JSON {"date": <UTC ISO-8601>, "sha": <40-char commit>,
# "deployer": <user>}. Host-side only -- never committed to the repo --
# so a missing or unreadable file simply means "never recorded".
DEPLOY_STAMP_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "DEPLOYED")


def deploy_stamp_path():
    return os.environ.get("DISPLAYD_DEPLOY_STAMP", DEPLOY_STAMP_FILE)


def read_deploy_stamp(path=None):
    """Last delivery stamp as a JSON-safe dict.

    Returns {"deployed": True, "date": ..., "sha": ..., "deployer": ...}
    when the stamp file holds JSON with a sha, else {"deployed": False}.
    Read-only and total: a missing or corrupt file is "never recorded",
    never an error."""
    try:
        with open(path or deploy_stamp_path()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"deployed": False}
    if not isinstance(data, dict) or not data.get("sha"):
        return {"deployed": False}
    return {"deployed": True,
            "date": data.get("date"),
            "sha": data.get("sha"),
            "deployer": data.get("deployer")}
# Reload confirmation (POST /reload): the QR payload is a panel-served
# one-time relay URL (GET /r/<token>) -- never the commit page directly,
# never the repository homepage, never a caller-supplied URL. Scanning the
# relay 302-redirects the scanner to the commit page AND confirms the view
# (the panel returns to whatever was showing). A screen tap while the
# reload view is showing confirms the same way (POST /reload/confirm).
# Mirrors renderers/reload.py COMMIT_URL_PREFIX and SHA_RE;
# tests/test_reload.py asserts the two agree so the rule cannot drift
# between validation and rendering.
RELOAD_COMMIT_URL_PREFIX = "https://github.com/trillium/displayd/commit/"
RELOAD_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# Reload confirmation (POST /reload) stays up indefinitely until dismissed
# by a touchscreen tap (POST /touch/tap) or cancelled by a manual /show or
# /clear -- it never expires by duration. The duration constants below are
# kept only to validate a legacy `duration` field when callers still send
# one (accepted, ignored for expiry); new clients should omit it.
RELOAD_DEFAULT_DURATION = None  # indefinite: no automatic return
RELOAD_DURATION_MIN, RELOAD_DURATION_MAX = 1, 300
# One-time relay tokens: url-safe, single-scan, valid only while the
# confirmation window is open (expiry == the reload duration). The token
# shape below is also the renderer's relay-URL acceptance rule, so the
# renderer can never be talked into encoding an arbitrary caller URL.
RELOAD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
RELOAD_RELAY_PATH = "/r/"
# Tailnet carrier-grade NAT range: the captain's phone and the panel meet
# here. The relay QR is only useful when the panel binds inside it.
TAILNET_CGNAT = "100.64.0.0/10"


def _relay_bind_host():
    """Address the panel binds (read live so tests can override the env)."""
    return (os.environ.get("DISPLAYD_BIND") or BIND or "127.0.0.1").strip()


def _relay_port():
    try:
        return int(os.environ.get("DISPLAYD_PORT") or PORT)
    except (TypeError, ValueError):
        return PORT


def relay_base_url():
    """(base, reachable, reason) for the panel-served relay URL.

    Reachability reasoning (explicit, per the task constraint): the
    captain scans with his phone on the tailnet (Tailscale), so the relay
    is reachable only when the panel itself binds a tailnet address
    (100.64.0.0/10 -- the address the panel already binds on lnx-server
    via DISPLAYD_BIND). A loopback bind (127.0.0.0/8, ::1, localhost),
    a wildcard bind (0.0.0.0 -- never used here: the API has no auth),
    a LAN literal, a public IP, or a hostname is NOT reachable from the
    phone, so the caller must fall back to tap-only and say so plainly --
    never serve a dead QR silently.
    """
    host = _relay_bind_host()
    port = _relay_port()
    bare = host.strip().strip("[]")
    try:
        addr = ipaddress.ip_address(bare.lower() if bare else "")
    except ValueError:
        if bare.lower() in ("localhost",) or bare.lower().endswith(".localhost"):
            return None, False, ("loopback bind %r: the phone on the tailnet "
                                 "cannot reach it; tap-only" % host)
        return None, False, ("non-IP bind %r: not a tailnet literal the phone "
                             "can reach; tap-only" % host)
    if addr.is_loopback:
        return None, False, ("loopback bind %r: the phone on the tailnet "
                             "cannot reach it; tap-only" % host)
    try:
        tailnet = ipaddress.ip_network(TAILNET_CGNAT)
    except ValueError:  # pragma: no cover - constant is fixed
        return None, False, "tailnet range misconfigured; tap-only"
    if addr in tailnet:
        return ("http://%s:%d" % (bare, port), True,
                "tailnet bind %s: reachable from the phone on the tailnet"
                % bare)
    if bare == "0.0.0.0":
        return None, False, ("wildcard bind: no single address the phone can "
                             "use; tap-only (and never bind 0.0.0.0: no auth)")
    return None, False, ("bind %r is outside %s: the phone on the tailnet "
                         "cannot reach it; tap-only" % (host, TAILNET_CGNAT))
