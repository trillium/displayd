"""/proc sampling for the resources renderer: pure parsers plus one poll.

Single concept: turn /proc text and statvfs numbers into a snapshot dict.
Everything here is side-effect free except _read (one file read) and
_poll_once (reads /proc, statvfs, hostname, cpu count). A missing /proc
file is a failed poll -- _poll_once raises, never returns a half frame.
"""

import os
import socket


# ---- sampling (pure functions over /proc text: unit-testable) ------------

def _parse_cpu_line(text):
    """First `cpu` line of /proc/stat -> (total, idle). Raises ValueError."""
    for line in str(text or "").splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            nums = [int(v) for v in parts[1:]]
            if len(nums) < 4:
                raise ValueError("short cpu line")
            total = sum(nums)
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
            return total, idle
    raise ValueError("no aggregate cpu line")


def _cpu_pct(prev, cur):
    """Delta between two (total, idle) samples -> 0..100 float, or None when
    the counters did not advance (first poll, or a wrapped/odd read)."""
    if prev is None or cur is None:
        return None
    d_total = cur[0] - prev[0]
    d_idle = cur[1] - prev[1]
    if d_total <= 0:
        return None
    pct = 100.0 * (1.0 - float(d_idle) / float(d_total))
    return max(0.0, min(100.0, pct))


def _parse_loadavg(text):
    """-> (l1, l5, l15, procs). Raises ValueError."""
    parts = str(text or "").split()
    if len(parts) < 4:
        raise ValueError("short loadavg")
    return float(parts[0]), float(parts[1]), float(parts[2]), parts[3]


def _parse_meminfo(text):
    """-> dict with mem/swap used+total in bytes. Prefers MemAvailable,
    falls back to Free+Buffers+Cached the way `free` does."""
    vals = {}
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        try:
            vals[key.strip()] = int(rest.strip().split()[0]) * 1024
        except (ValueError, IndexError):
            continue
    if "MemTotal" not in vals:
        raise ValueError("no MemTotal")
    total = vals["MemTotal"]
    avail = vals.get("MemAvailable")
    if avail is None:
        avail = vals.get("MemFree", 0) + vals.get("Buffers", 0) + vals.get("Cached", 0)
    swap_total = vals.get("SwapTotal", 0)
    swap_free = vals.get("SwapFree", 0)
    return {
        "total": total,
        "used": max(0, total - avail),
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
    }


def _read(path):
    with open(path, "r") as fh:
        return fh.read()


def _poll_once(cfg, prev_cpu):
    """One poll. Returns (snapshot, new_prev_cpu). Raises on failure; a
    missing /proc file is a failed poll, never a crash."""
    total, idle = _parse_cpu_line(_read("/proc/stat"))
    cur = (total, idle)
    pct = _cpu_pct(prev_cpu, cur)
    load = _parse_loadavg(_read("/proc/loadavg"))
    mem = _parse_meminfo(_read("/proc/meminfo"))
    try:
        uptime_s = float(_read("/proc/uptime").split()[0])
    except (ValueError, IndexError, OSError):
        uptime_s = 0.0
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "?"
    try:
        ncpu = os.cpu_count() or 1
    except Exception:
        ncpu = 1

    disks = []
    seen_devs = set()
    for mount in [m.strip() for m in str(cfg.get("mounts") or "/").split(",") if m.strip()]:
        try:
            st = os.statvfs(mount)
            dev = None
            try:
                dev = os.stat(mount).st_dev
            except OSError:
                pass
            if dev is not None and dev in seen_devs:
                continue  # same filesystem twice: show it once
            if dev is not None:
                seen_devs.add(dev)
            total_b = st.f_frsize * st.f_blocks
            # f_bfree (not f_bavail): matches `df` Used/Use% exactly, so the
            # wall agrees with the terminal. Reserved blocks count as used.
            used_b = max(0, total_b - st.f_frsize * st.f_bfree)
            disks.append({
                "mount": mount,
                "used": used_b,
                "total": total_b,
                "pct": (100.0 * used_b / total_b) if total_b else 0.0,
            })
        except OSError as err:
            disks.append({"mount": mount, "error": str(err)[:80]})
    if not disks:
        disks.append({"mount": "/", "error": "no mounts configured"})

    snap = {
        "cpu_pct": pct,  # None until the second poll banks a delta
        "load": load[:3],
        "procs": load[3],
        "mem_used": mem["used"],
        "mem_total": mem["total"],
        "swap_used": mem["swap_used"],
        "swap_total": mem["swap_total"],
        "disks": disks,
        "uptime_s": uptime_s,
        "hostname": hostname,
        "ncpu": ncpu,
    }
    return snap, cur
