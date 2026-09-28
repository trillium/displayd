"""Inventory shaping for the services renderer.

Single concept: shape raw lnx-viz inventory JSON into what the wall
needs. Pure over decoded JSON: unit-testable, no I/O, no threads.
"""

DEFAULT_WATCH = "displayd.service,lnx-viz.service,docker.service,containerd.service"


def _build_snapshot(data, watch):
    """Shape raw inventory JSON into what the wall needs. Raises ValueError
    on a missing or malformed payload."""
    if not isinstance(data, dict):
        raise ValueError("inventory is not an object")
    services = data.get("services")
    if not isinstance(services, list):
        raise ValueError("inventory has no services list")
    containers = data.get("containers")
    if not isinstance(containers, list):
        containers = []
    listeners = data.get("listeners")
    if not isinstance(listeners, list):
        listeners = []

    up = down = failed = 0
    failed_units = []
    by_name = {}
    for svc in services:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("name") or "?")
        state = str(svc.get("state") or "").strip().lower()
        by_name[name] = {
            "state": state,
            "detail": str(svc.get("detail") or ""),
            "uptime": str(svc.get("uptime") or ""),
            "restarts": str(svc.get("restarts") or "0"),
        }
        if state == "up":
            up += 1
        elif state == "failed":
            failed += 1
            failed_units.append(name)
        else:
            down += 1
    failed_units.sort()

    watched = []
    for name in [w.strip() for w in str(watch or "").split(",") if w.strip()]:
        info = by_name.get(name)
        if info is None:
            watched.append({"name": name, "state": "unknown", "detail": "not in inventory"})
        else:
            watched.append({"name": name, **info})

    conts = []
    for c in containers:
        if not isinstance(c, dict):
            continue
        conts.append({
            "name": str(c.get("name") or "?"),
            "state": str(c.get("state") or "?").strip().lower(),
            "status": str(c.get("status") or ""),
        })

    # Notable listening ports: dedupe by port (0.0.0.0:22 and [::]:22 are
    # one line on a wall), skip ephemeral tailscale noise, cap at 8. The
    # panel's own neighbours come first: when the wall runs out of room
    # it is the high-numbered strangers that drop off, never these.
    PRIORITY = (8181, 8980, 7080, 22, 53, 631, 443)
    seen_ports, ports = set(), []
    valid = []
    for l in listeners:
        if not isinstance(l, dict):
            continue
        try:
            port = int(l.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if not port or port in seen_ports or port >= 30000:
            continue
        seen_ports.add(port)
        valid.append({"port": port,
                      "process": str(l.get("process") or "?")[:24]})

    def _rank(p):
        try:
            return PRIORITY.index(p["port"])
        except ValueError:
            return len(PRIORITY) + p["port"] / 100000.0
    ports = sorted(valid, key=_rank)[:8]

    return {
        "up": up,
        "down": down,
        "failed": failed,
        "total": up + down + failed,
        "failed_units": failed_units,
        "watched": watched,
        "containers": conts,
        "ports": ports,
        "host_uptime": str(data.get("host_uptime") or ""),
        "hostname": str(data.get("hostname") or ""),
        "generated_at": data.get("generated_at"),
    }
