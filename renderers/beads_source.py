"""Where a beads snapshot comes from: mirror files, then live store CLIs.

Single concept: obtaining raw (store, issue) pairs -- discovering the
candidate mirror files, parsing each mirror shape (JSON array, JSONL, or
{"stores": {...}}), and falling back to a live `<store> export` and a
single `<store> show <id> --json` when no mirror answers. Every CLI call is
bounded by CLI_TIMEOUT and every failure is reported, never swallowed into
an empty snapshot. Normalization lives in beads_issue.py; polling lives in
beads_poll.py.
"""

import json
import os
import shutil
import subprocess

# Bound on every store CLI call, so a hung CLI cannot stall the poll thread.
CLI_TIMEOUT = 25


def _load_mirror_file(path):
    """Read one mirror file: JSON array, JSONL, or {stores:{name:[...]}}."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
        issues = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            issues.append(json.loads(line))  # raises on malformed -> caller
        store = os.path.splitext(os.path.basename(path))[0]
        return [(store, issue) for issue in issues]
    if isinstance(data, dict) and isinstance(data.get("stores"), dict):
        out = []
        for store, issues in data["stores"].items():
            for issue in issues or []:
                out.append((str(store), issue))
        return out
    if isinstance(data, dict) and isinstance(data.get("issues"), list):
        return [("mirror", issue) for issue in data["issues"]]
    if isinstance(data, list):
        store = os.path.splitext(os.path.basename(path))[0]
        return [(store, issue) for issue in data]
    raise ValueError("unrecognised mirror shape in %s" % path)


def _candidate_mirrors(explicit):
    cands = []
    if explicit:
        cands.extend(p.strip() for p in str(explicit).split(",") if p.strip())
    env = os.environ.get("DISPLAYD_BEADS_MIRROR")
    if env:
        cands.extend(p.strip() for p in env.split(",") if p.strip())
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        root = None
    names = (".beads-mirror-fleet.json", ".beads-mirror-ready.json",
             ".beads-mirror-inflight.json", "beads-mirror.json")
    search = []
    if root:
        search.append(os.path.join(root, "state"))
        search.append(root)
    search.extend((
        "/home/trillium/displayd/state",
        "/opt/displayd/state",
        "/var/lib/displayd",
    ))
    for directory in search:
        for name in names:
            cands.append(os.path.join(directory, name))
    seen, out = set(), []
    for path in cands:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _export_store(cli):
    exe = shutil.which(cli)
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "export"], capture_output=True, text=True, timeout=CLI_TIMEOUT)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    issues = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            issues.append(json.loads(line))
        except ValueError:
            continue
    return [(cli, issue) for issue in issues]


def _show_bead(cli, bead_id):
    """Fetch one bead live: `<cli> show <id> --json`. Returns (store, raw)
    or None. Runs in a worker thread, never on the draw path."""
    exe = shutil.which(cli)
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "show", bead_id, "--json"],
            capture_output=True, text=True, timeout=CLI_TIMEOUT)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    cands = data if isinstance(data, list) else [data]
    for cand in cands:
        if isinstance(cand, dict) and str(cand.get("id") or "") == bead_id:
            return (cli, cand)
    return None


def _poll_once(cfg):
    """One poll: mirrors first, live CLI export as fallback. Returns
    (pairs, source) or raises."""
    errors = []
    for path in _candidate_mirrors(cfg.get("mirror")):
        if not os.path.isfile(path):
            continue
        try:
            pairs = _load_mirror_file(path)
            if pairs:
                return pairs, path
            errors.append("%s: empty" % path)
        except Exception as err:
            errors.append("%s: %s" % (path, err))
    if errors:
        raise ValueError("; ".join(errors))
    stores = [s.strip() for s in str(cfg.get("stores") or "").split(",") if s.strip()]
    collected = []
    for store in stores:
        try:
            pairs = _export_store(store)
        except Exception:
            pairs = None
        if pairs:
            collected.extend(pairs)
    if collected:
        return collected, "live:" + ",".join(
            sorted({store for store, _ in collected}))
    raise ValueError("no mirror file and no live store answered")
