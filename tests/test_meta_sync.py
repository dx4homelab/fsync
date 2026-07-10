"""Meta-synchronization: config `include:` propagation + `requires:` gate +
`fsync meta version|check|status`."""

from __future__ import annotations

import json
from subprocess import CompletedProcess

import pytest


def test_config_include_merge(tmp_path, monkeypatch):
    """Shared profiles come from the included file; box-local peer/direction from
    the main file overlay and win."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from fsync.homesync import load_config

    (tmp_path / "shared.yaml").write_text(
        "requires: [git-sync]\n"
        "defaults: {conflict: newer, direction: both}\n"
        "profiles:\n"
        "  repos:\n    kind: git\n    paths: [workspaces/primary]\n"
        "  docs:\n    paths: [Documents]\n"
    )
    main = tmp_path / "main.yaml"
    main.write_text(
        "include: [shared.yaml]\n"
        "peer: {host: fury4dx.lan, user: developer, home: /home/developer}\n"
        "defaults: {direction: pull}\n"          # box-local overlay
    )
    peer, profiles, defaults = load_config(str(main))
    assert peer.host == "fury4dx.lan"            # box-local peer from main
    assert set(profiles) == {"repos", "docs"}     # profiles from the shared include
    assert profiles["repos"].kind == "git"
    assert profiles["docs"].direction == "pull"   # box-local direction overlay wins


def test_requires_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from fsync.homesync import HomesyncError, load_config

    bad = tmp_path / "bad.yaml"
    bad.write_text("requires: [time-travel]\npeer: {host: h}\nprofiles:\n  x: {paths: [a]}\n")
    with pytest.raises(HomesyncError) as e:
        load_config(str(bad))
    assert "time-travel" in str(e.value)

    good = tmp_path / "good.yaml"
    good.write_text("requires: [git-sync, meta-sync]\npeer: {host: h}\nprofiles:\n  x: {paths: [a]}\n")
    _peer, profiles, _d = load_config(str(good))
    assert "x" in profiles


def test_meta_version_and_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    from fsync import FEATURES, __version__
    from fsync.cli import main

    cfg = tmp_path / ".config" / "fsync" / "sync-profiles.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("requires: [git-sync]\npeer: {host: h}\n"
                   "profiles:\n  repos: {kind: git, paths: [workspaces/primary]}\n")

    assert main(["meta", "version", "--config", str(cfg)]) == 0
    man = json.loads(capsys.readouterr().out)
    assert man["fsync_version"] == __version__
    assert set(man["features"]) == set(FEATURES)
    assert man["config"]["git_profiles"] == ["repos"]
    assert man["config"]["requires"] == ["git-sync"]

    assert main(["meta", "check", "--config", str(cfg)]) == 0
    assert "config: OK" in capsys.readouterr().out


def _peer_ssh(manifest):
    return lambda peer, cmd, **k: CompletedProcess([], 0, stdout=json.dumps(manifest), stderr="")


def test_meta_status_compatible(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    import fsync.homesync as hs

    cfg = tmp_path / "c.yaml"
    cfg.write_text("requires: [git-sync]\npeer: {host: peer.lan, user: d, home: /h}\n"
                   "profiles:\n  repos: {kind: git, paths: [p]}\n")
    monkeypatch.setattr(hs, "_ssh", _peer_ssh(
        {"fsync_version": hs.FSYNC_VERSION, "features": sorted(hs.FEATURES),
         "config": {"ok": True, "requires": ["git-sync"]}}))
    assert hs._meta_status(str(cfg)) == 0
    assert "compatible" in capsys.readouterr().out


def test_meta_status_peer_missing_feature(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    import fsync.homesync as hs

    cfg = tmp_path / "c.yaml"
    cfg.write_text("requires: [git-sync]\npeer: {host: peer.lan}\n"
                   "profiles:\n  repos: {kind: git, paths: [p]}\n")
    monkeypatch.setattr(hs, "_ssh", _peer_ssh(
        {"fsync_version": "0.1.0", "features": ["home-sync"], "config": {"ok": True}}))
    assert hs._meta_status(str(cfg)) == 1
    err = capsys.readouterr().err
    assert "peer missing" in err and "git-sync" in err


def test_meta_status_peer_old_build(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    import fsync.homesync as hs

    cfg = tmp_path / "c.yaml"
    cfg.write_text("peer: {host: peer.lan}\nprofiles:\n  x: {paths: [a]}\n")
    monkeypatch.setattr(hs, "_ssh",
                        lambda peer, cmd, **k: CompletedProcess([], 1, stdout="", stderr="not found"))
    assert hs._meta_status(str(cfg)) == 1
    assert "unavailable" in capsys.readouterr().err
