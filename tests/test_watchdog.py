"""0.10.3: deploy daemon survives a target Python bump; sync watchdog."""
import json
import subprocess
from subprocess import CompletedProcess

import fsync.homesync as hs


def test_daemon_deploy_script_is_valid_sh_and_installs_by_path():
    script = hs._daemon_deploy_script()
    assert subprocess.run(["bash", "-n", "-c", script], capture_output=True).returncode == 0
    assert '--find-links "$WH" "$WHL"' in script          # wheel by path
    assert '"$WH" fsync ' not in script                     # never by name
    assert script.index("build --no-index") < script.index("build --only-binary :all:;")
    assert "PYYAML_FORCE_LIBYAML=0 build --only-binary :all: --no-binary pyyaml" in script
    assert "sync watchdog install" in script


def test_render_watchdog_units():
    service, timer = hs.render_watchdog_units(["/usr/bin/env", "python3", "/h/.local/bin/fsync"], "1h")
    assert "ExecStart=/usr/bin/env python3 /h/.local/bin/fsync sync watchdog check" in service
    assert "Type=oneshot" in service and "DBUS_SESSION_BUS_ADDRESS" in service
    assert "OnUnitInactiveSec=1h" in timer
    assert not any(line.startswith("OnUnitActiveSec") for line in timer.splitlines())


def _wire(monkeypatch, tmp_path, enabled=True, states=("active",)):
    seq = list(states)
    notes, runs = [], []

    def fake_systemctl(*argv):
        if argv[0] == "is-enabled":
            return CompletedProcess(argv, 0 if enabled else 1, stdout="", stderr="")
        if argv[0] == "is-active":
            s = seq.pop(0) if len(seq) > 1 else seq[0]
            return CompletedProcess(argv, 0 if s == "active" else 3, stdout=s + "\n", stderr="")
        return CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(hs, "_systemctl", fake_systemctl)
    monkeypatch.setattr(hs, "_notify", lambda s, b: notes.append((s, b)))
    monkeypatch.setattr(hs, "state_root", lambda: tmp_path)
    return notes, runs


def test_watchdog_healthy_and_not_installed(monkeypatch, tmp_path):
    notes, runs = _wire(monkeypatch, tmp_path)
    assert hs.watchdog_check(settle=0, run_sync=lambda: runs.append(1) or 0, log=lambda m: None) \
        == {"daemon": "active", "action": "none"}
    notes2, _ = _wire(monkeypatch, tmp_path, enabled=False, states=("inactive",))
    assert hs.watchdog_check(settle=0, run_sync=lambda: runs.append(1) or 0)["daemon"] == "not-installed"
    assert not notes and not notes2 and not runs


def test_watchdog_down_notifies_once_runs_every_time_then_recovers(monkeypatch, tmp_path):
    notes, runs = _wire(monkeypatch, tmp_path, states=("activating",))
    run = lambda: runs.append(1) or 0                                   # noqa: E731
    r1 = hs.watchdog_check(settle=0, now=1000.0, run_sync=run, log=lambda m: None)
    assert r1["action"] == "fallback-run" and r1["notified"] and r1["run_rc"] == 0
    r2 = hs.watchdog_check(settle=0, now=1000.0 + 3600, run_sync=run, log=lambda m: None)
    assert r2["action"] == "fallback-run" and not r2["notified"]
    assert len(notes) == 1 and "DOWN" in notes[0][0] and len(runs) == 2
    assert json.loads((tmp_path / "watchdog.json").read_text())["down_since"] == 1000.0
    # a day later it reminds again
    r3 = hs.watchdog_check(settle=0, now=1000.0 + 25 * 3600, run_sync=run, log=lambda m: None)
    assert r3["notified"] and len(notes) == 2
    # daemon back: state cleared, no run
    _wire(monkeypatch, tmp_path, states=("active",))
    assert hs.watchdog_check(settle=0, run_sync=run, log=lambda m: None)["action"] == "none"
    assert json.loads((tmp_path / "watchdog.json").read_text()) == {} and len(runs) == 3


def test_watchdog_restart_in_progress_is_not_an_outage(monkeypatch, tmp_path):
    notes, runs = _wire(monkeypatch, tmp_path, states=("activating", "active"))
    monkeypatch.setattr(hs.time, "sleep", lambda s: None)
    assert hs.watchdog_check(settle=1, run_sync=lambda: runs.append(1) or 0)["daemon"] == "active"
    assert not notes and not runs
