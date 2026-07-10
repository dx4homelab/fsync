"""P6 meta-deploy: multi-host config resolution, pyz build, deploy targets."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import zipfile
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# multi-host config resolution                                                #
# --------------------------------------------------------------------------- #

def _hosts_config(tmp_path, requires="[git-sync]"):
    cfg = tmp_path / "hosts.yaml"
    cfg.write_text(
        f"version: 1\nrequires: {requires}\n"
        "hosts:\n"
        "  fury4dx:  {peer: {host: minis.lan, user: dev, home: /h}, defaults: {direction: push}}\n"
        "  minis4dx: {peer: {host: fury.lan,  user: dev, home: /h}, defaults: {direction: pull}}\n"
        "shared:\n"
        "  defaults: {conflict: newer}\n"
        "  profiles:\n"
        "    repos: {kind: git, paths: [workspaces/primary]}\n"
        "    docs:  {paths: [Documents]}\n")
    return cfg


def test_multi_host_selects_by_hostname(tmp_path, monkeypatch):
    from fsync.homesync import load_config
    cfg = _hosts_config(tmp_path)

    monkeypatch.setattr(socket, "gethostname", lambda: "fury4dx")
    peer, profiles, _d = load_config(str(cfg))
    assert peer.host == "minis.lan"
    assert profiles["repos"].direction == "push"          # fury's box-local direction
    assert profiles["docs"].direction == "push"

    monkeypatch.setattr(socket, "gethostname", lambda: "minis4dx.lan")  # short-name match
    peer2, profiles2, _d2 = load_config(str(cfg))
    assert peer2.host == "fury.lan"
    assert profiles2["repos"].direction == "pull"


def test_multi_host_unknown_host_errors(tmp_path, monkeypatch):
    from fsync.homesync import HomesyncError, load_config
    cfg = _hosts_config(tmp_path)
    monkeypatch.setattr(socket, "gethostname", lambda: "stranger")
    with pytest.raises(HomesyncError) as e:
        load_config(str(cfg))
    assert "not in the config hosts" in str(e.value)


def test_multi_host_requires_gate_fires(tmp_path, monkeypatch):
    from fsync.homesync import HomesyncError, load_config
    cfg = _hosts_config(tmp_path, requires="[warp-drive]")
    monkeypatch.setattr(socket, "gethostname", lambda: "fury4dx")
    with pytest.raises(HomesyncError) as e:
        load_config(str(cfg))
    assert "warp-drive" in str(e.value)


def test_single_host_config_still_parses(tmp_path, monkeypatch):
    """Backward compat: a plain (no hosts:) config is unaffected by P6."""
    from fsync.homesync import load_config
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "plain.yaml"
    cfg.write_text("peer: {host: h}\nprofiles:\n  p: {paths: [a]}\n")
    peer, profiles, _d = load_config(str(cfg))
    assert peer.host == "h" and "p" in profiles


# --------------------------------------------------------------------------- #
# deploy targets                                                              #
# --------------------------------------------------------------------------- #

def test_remote_targets_2box_peer_fallback():
    from fsync.meta_deploy import remote_targets
    data = {"hosts": {
        "a": {"peer": {"host": "b.lan", "user": "dev"}},
        "b": {"peer": {"host": "a.lan", "user": "dev"}},
    }}
    assert remote_targets(data, "a") == [("b", "dev@b.lan")]          # a's peer is b
    assert remote_targets(data, "b.dom") == [("a", "dev@a.lan")]      # short-name match


def test_remote_targets_explicit_ssh_multi():
    from fsync.meta_deploy import remote_targets
    data = {"hosts": {
        "a": {"ssh": "dev@a.ex"}, "b": {"ssh": "dev@b.ex"}, "c": {"ssh": "dev@c.ex"},
    }}
    assert dict(remote_targets(data, "a")) == {"b": "dev@b.ex", "c": "dev@c.ex"}


def test_remote_targets_missing_ssh_errors():
    from fsync.meta_deploy import DeployError, remote_targets
    data = {"hosts": {"a": {}, "b": {}, "c": {}}}   # >2 hosts, no ssh, no fallback
    with pytest.raises(DeployError):
        remote_targets(data, "a")


# --------------------------------------------------------------------------- #
# pyz build                                                                   #
# --------------------------------------------------------------------------- #

def test_build_pyz_contents(tmp_path):
    from fsync import meta_deploy as md
    res = md.build_pyz(tmp_path / "fsync.pyz")
    p = Path(res["path"])
    assert p.exists() and os.access(p, os.X_OK)
    names = zipfile.ZipFile(p).namelist()
    assert any(n.startswith("yaml/") for n in names)              # PyYAML vendored
    assert any(n.endswith("fsync/homesync.py") for n in names)
    assert any(n.endswith("fsync/git_repo_sync.py") for n in names)
    assert not any(n.endswith("fsync/web.py") for n in names)     # UI omitted
    assert not any(n.endswith("fsync/gtk_app.py") for n in names)
    assert "__main__.py" in names


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="needs system python3")
def test_pyz_runs_headless(tmp_path):
    """The pyz runs on a bare python3 (no repo/venv) and self-selects the host."""
    from fsync import meta_deploy as md
    res = md.build_pyz(tmp_path / "fsync.pyz")

    home = tmp_path / "home"
    (home / ".config" / "fsync").mkdir(parents=True)
    host = socket.gethostname().split(".")[0]
    (home / ".config" / "fsync" / "sync-profiles.yaml").write_text(
        "requires: [git-sync]\n"
        f"hosts:\n  {host}: {{peer: {{host: peer.lan, user: dev}}, defaults: {{direction: pull}}}}\n"
        "shared:\n  profiles:\n    repos: {kind: git, paths: [workspaces/primary]}\n")

    p = subprocess.run(["/usr/bin/python3", res["path"], "meta", "version"],
                       capture_output=True, text=True, env={**os.environ, "HOME": str(home)})
    assert p.returncode == 0, p.stderr
    man = json.loads(p.stdout)
    assert man["fsync_version"]
    assert "git-sync" in man["features"]
    assert man["config"]["ok"] is True
    assert man["config"]["peer"].endswith("peer.lan")      # host self-selected


def test_deploy_build_and_dry_run_via_main(tmp_path, monkeypatch):
    from fsync.cli import main
    out = tmp_path / "f.pyz"
    assert main(["deploy", "build", "--out", str(out)]) == 0
    assert out.exists()

    monkeypatch.setattr(socket, "gethostname", lambda: "boxA")
    cfg = tmp_path / "hosts.yaml"
    cfg.write_text(
        "requires: [git-sync]\n"
        "hosts:\n"
        "  boxA: {ssh: dev@a.lan, peer: {host: b.lan, user: dev}, defaults: {direction: push}}\n"
        "  boxB: {ssh: dev@b.lan, peer: {host: a.lan, user: dev}, defaults: {direction: pull}}\n"
        "shared:\n  profiles:\n    repos: {kind: git, paths: [p]}\n")
    assert main(["deploy", "push", "--config", str(cfg), "--dry-run", "--out", str(out)]) == 0
