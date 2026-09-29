"""Mac-side bridge install set (install-mac.sh / verify-mac-install.sh).

Pins the incident fix (task-va5wb): the bridges deploy as a SET, every job
has launchd coverage or a documented ride-inside reason, and a partial
install fails loudly at install time — never silently degrades.
"""
import json
import os
import plistlib
import shutil
import subprocess
import tempfile

import pytest

BRIDGES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "bridges")


def manifest_rows():
    rows = []
    with open(os.path.join(BRIDGES, "mac-set.manifest")) as fh:
        for line in fh:
            line = line.split("#")[0].strip() if line.lstrip().startswith("#") else line
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, role = line.split()
            rows.append((name, role))
    return rows


def test_manifest_files_exist_and_service_excluded():
    rows = manifest_rows()
    assert len(rows) >= 10, "manifest should cover the whole Mac set"
    for name, _role in rows:
        assert os.path.isfile(os.path.join(BRIDGES, name)), name
    # Panel-host only: the systemd unit must never join the Mac set.
    names = [n for n, _ in rows]
    assert "firebot-chat-bridge.service" not in names
    assert os.path.isfile(os.path.join(
        BRIDGES, "firebot-chat-bridge.service"))


def test_every_job_has_plist_template():
    for _name, role in manifest_rows():
        if not role.startswith("job:"):
            continue
        label = role.split("job:", 1)[1]
        assert os.path.isfile(os.path.join(BRIDGES, label + ".plist")), label


def test_plist_conventions():
    for _name, role in manifest_rows():
        if not role.startswith("job:"):
            continue
        label = role.split("job:", 1)[1]
        with open(os.path.join(BRIDGES, label + ".plist"), "rb") as fh:
            doc = plistlib.load(fh)
        assert doc["Label"] == label
        prog = doc["ProgramArguments"]
        assert any("__DISPLAYD_DIR__" in a for a in prog), label
        assert doc.get("RunAtLoad") is True, label
        assert doc.get("KeepAlive") is True, label


def test_preview_and_zoom_ride_inside_state_job():
    # No plist may exist for the preview/zoom loops: they run inside
    # macos_state.py (daemon thread / tap hooks).
    names = os.listdir(BRIDGES)
    assert not any("preview" in n and n.endswith(".plist") for n in names)
    assert not any("zoom" in n and n.endswith(".plist") for n in names)
    with open(os.path.join(BRIDGES, "macos_state.py")) as fh:
        src = fh.read()
    assert "mac_preview.start(" in src


def test_forgiving_guards_unchanged():
    # Runtime behaviour is pinned: a missing companion degrades, never dies.
    with open(os.path.join(BRIDGES, "macos_state.py")) as fh:
        src = fh.read()
    assert "mac_preview = None" in src
    assert "mac_zoom = None" in src


def test_installer_renders_flat_dest_and_plists():
    dest = tempfile.mkdtemp(prefix="mac-install-")
    agents = tempfile.mkdtemp(prefix="mac-agents-")
    try:
        proc = subprocess.run(
            ["sh", os.path.join(BRIDGES, "install-mac.sh"),
             "--dir", dest, "--agents-dir", agents,
             "--no-restart", "--no-verify"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "deployed 16 files" in proc.stdout
        for name, _role in manifest_rows():
            src = os.path.join(BRIDGES, name)
            got = os.path.join(dest, name)
            assert os.path.isfile(got), name
            with open(src, "rb") as fh1, open(got, "rb") as fh2:
                assert fh1.read() == fh2.read(), name
        plist = os.path.join(agents,
                             "com.displayd.macos-state-bridge.plist")
        with open(plist) as fh:
            rendered = fh.read()
        assert "<string>__DISPLAYD_DIR__" not in rendered
        assert ("<string>%s/macos_state.py</string>" % dest) in rendered
    finally:
        shutil.rmtree(dest, ignore_errors=True)
        shutil.rmtree(agents, ignore_errors=True)


def _run_verify(dest, state_doc, jobs):
    state_path = os.path.join(dest, "state.json")
    with open(state_path, "w") as fh:
        json.dump(state_doc, fh)
    proc = subprocess.run(
        ["sh", os.path.join(BRIDGES, "verify-mac-install.sh"),
         "--dir", dest, "--jobs", jobs, "--state-json", state_path],
        capture_output=True, text=True, timeout=60)
    return proc


def _warm_state():
    def feed(health, age):
        return {"health": health, "age_seconds": age}
    return {"feeds": {
        "macbook": {"state": feed("warm", 0.5), "preview": feed("warm", 1.0),
                    "zoom": feed("cold", None)},
        "talon_apps": {"state": feed("warm", 1.5)},
        "chat": {"message": feed("cold", None)},
        "activity": {"event": feed("cold", None)},
        "stream": {"frame": feed("cold", None)},
    }}


def _deploy_full(dest):
    os.makedirs(dest, exist_ok=True)
    for name, _role in manifest_rows():
        shutil.copy(os.path.join(BRIDGES, name), os.path.join(dest, name))


def test_verify_passes_on_whole_set_with_warm_feeds():
    dest = tempfile.mkdtemp(prefix="mac-set-")
    try:
        _deploy_full(dest)
        proc = _run_verify(dest, _warm_state(),
                           "com.displayd.macos-state-bridge,"
                           "com.displayd.talon-apps-bridge")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "VERIFY PASS" in proc.stdout
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def test_verify_catches_withheld_file():
    # The incident replayed: mac_preview.py withheld from the deployed set.
    dest = tempfile.mkdtemp(prefix="mac-set-partial-")
    try:
        _deploy_full(dest)
        os.unlink(os.path.join(dest, "mac_preview.py"))
        state = _warm_state()
        state["feeds"]["macbook"]["preview"] = {"health": "cold",
                                                "age_seconds": None}
        proc = _run_verify(dest, state,
                           "com.displayd.macos-state-bridge")
        assert proc.returncode != 0
        assert "mac_preview.py missing" in proc.stdout + proc.stderr
        assert "preview" in proc.stdout + proc.stderr
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def test_verify_catches_stale_file():
    dest = tempfile.mkdtemp(prefix="mac-set-stale-")
    try:
        _deploy_full(dest)
        with open(os.path.join(dest, "macos_state.py"), "a") as fh:
            fh.write("\n# stale\n")
        proc = _run_verify(dest, _warm_state(),
                           "com.displayd.macos-state-bridge")
        assert proc.returncode != 0
        assert "STALE" in proc.stdout + proc.stderr
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def test_verify_catches_cold_feed_on_whole_set():
    # Whole files, but the preview thread never started: still loud.
    dest = tempfile.mkdtemp(prefix="mac-set-coldfeed-")
    try:
        _deploy_full(dest)
        state = _warm_state()
        state["feeds"]["macbook"]["preview"] = {"health": "cold",
                                                "age_seconds": None}
        proc = _run_verify(dest, state,
                           "com.displayd.macos-state-bridge")
        assert proc.returncode != 0
        assert "macbook/preview" in proc.stdout
        assert "SILENTLY" in proc.stdout
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def test_event_driven_feeds_never_gate():
    # Quiet night: no chat, no activity, no stream, no zoom — still PASS.
    dest = tempfile.mkdtemp(prefix="mac-set-quiet-")
    try:
        _deploy_full(dest)
        proc = _run_verify(dest, _warm_state(),
                           "com.displayd.macos-state-bridge,"
                           "com.displayd.talon-apps-bridge,"
                           "com.displayd.firebot-chat-bridge,"
                           "com.displayd.beads-activity-bridge,"
                           "com.displayd.obs-stream-bridge")
        assert proc.returncode == 0, proc.stdout + proc.stderr
    finally:
        shutil.rmtree(dest, ignore_errors=True)
