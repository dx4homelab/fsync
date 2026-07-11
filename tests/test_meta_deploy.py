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


def test_remote_targets_fqdn_keys_short_self_excludes_self():
    """Review #3a: FQDN config keys + short gethostname must still identify
    self — else self becomes a deploy target and its install is clobbered."""
    from fsync.meta_deploy import remote_targets
    data = {"hosts": {
        "minis4dx.lan": {"ssh": "dev@minis4dx.lan"},
        "fury4dx.lan": {"ssh": "dev@fury4dx.lan"},
    }}
    assert remote_targets(data, "minis4dx") == [("fury4dx.lan", "dev@fury4dx.lan")]


def test_remote_targets_unknown_self_refuses():
    """Self not in hosts: refuse outright rather than treating every host
    (including possibly self) as a remote."""
    from fsync.meta_deploy import DeployError, remote_targets
    data = {"hosts": {"a": {"ssh": "d@a"}, "b": {"ssh": "d@b"}}}
    with pytest.raises(DeployError) as e:
        remote_targets(data, "stranger")
    assert "refusing" in str(e.value)


def test_remote_targets_self_pointing_peer_refused():
    """Review #3b: a misconfigured peer that points back at self must not
    become a deploy target."""
    from fsync.meta_deploy import DeployError, remote_targets
    data = {"hosts": {
        "a": {"peer": {"host": "a.lan", "user": "dev"}},   # a's peer is itself
        "b": {},
    }}
    with pytest.raises(DeployError) as e:
        remote_targets(data, "a")
    assert "self-deploy" in str(e.value)


# --------------------------------------------------------------------------- #
# _deploy_to: backup-first + pair rollback (review findings #1, #2, #4)        #
# --------------------------------------------------------------------------- #

from subprocess import CompletedProcess


class _FakeRemote:
    """Records ssh commands + rsync pushes; fails any command matching `fail`.
    Distinguishing substrings: backup cp ends '<file>.bak; fi', restore cp is
    'cp -f ~/<file>.bak ~/<file>' (contains '.bak ~/')."""
    def __init__(self, fail=lambda cmd: False):
        self.cmds, self.pushed, self.fail = [], [], fail

    def ssh(self, target, cmd, **kw):
        self.cmds.append(cmd)
        if self.fail(cmd):
            return CompletedProcess([], 1, stdout="", stderr="boom")
        out = '{"fsync_version": "0.2.0"}' if "meta version" in cmd else ""
        return CompletedProcess([], 0, stdout=out, stderr="")

    def push(self, src, target, dst):
        self.pushed.append(dst)


def _wire(monkeypatch, fake):
    import fsync.homesync as hs
    monkeypatch.setattr(hs, "_ssh_to", fake.ssh)
    monkeypatch.setattr(hs, "_rsync_push", fake.push)
    return hs


def _is_restore(cmd):
    return ".bak ~/" in cmd            # cp FROM .bak back into place


def test_deploy_to_happy_path_backs_up_before_overwrite(monkeypatch):
    fake = _FakeRemote()
    hs = _wire(monkeypatch, fake)
    res = hs._deploy_to("dev@remote", "p.pyz", "c.yaml")
    assert res["verify_rc"] == 0 and res["rolled_back"] is False
    assert fake.pushed == [".local/bin/fsync.tmp",
                           ".config/fsync/sync-profiles.yaml.tmp"]   # temp, then mv
    backup_idx = next(i for i, c in enumerate(fake.cmds) if ".bak; fi" in c)
    mv_idx = next(i for i, c in enumerate(fake.cmds) if c.startswith("chmod +x"))
    assert backup_idx < mv_idx                       # backup strictly first
    assert not any(_is_restore(c) for c in fake.cmds)  # no rollback ran


def test_deploy_to_verify_failure_rolls_back(monkeypatch):
    """Review #1 (brick): failed verify must restore the previous install."""
    fake = _FakeRemote(fail=lambda c: "meta version" in c)
    hs = _wire(monkeypatch, fake)
    res = hs._deploy_to("dev@remote", "p.pyz", "c.yaml")
    assert res["verify_rc"] != 0
    assert res["rolled_back"] is True
    assert any(_is_restore(c) for c in fake.cmds)


def test_deploy_to_partial_mv_rolls_back_and_raises(monkeypatch):
    """Review #4: a failed install mv must restore the PAIR, not leave a new
    binary with an old config."""
    from fsync.meta_deploy import DeployError
    fake = _FakeRemote(fail=lambda c: c.startswith("chmod +x"))
    hs = _wire(monkeypatch, fake)
    with pytest.raises(DeployError) as e:
        hs._deploy_to("dev@remote", "p.pyz", "c.yaml")
    assert "rolled back" in str(e.value)
    assert any(_is_restore(c) for c in fake.cmds)


def test_engine_push_args_source_vs_pyz(tmp_path):
    """P6.4 live-test finding: from a zipapp the engine push must send the .pyz
    itself (the package 'dir' is a zip member rsync can't read)."""
    from fsync.homesync import ENGINE_DIR, HomesyncError, _engine_push_args

    pkg = tmp_path / "fsync"; pkg.mkdir()                 # source install: a real dir
    src_args, dst = _engine_push_args(pkg)
    assert dst == f"{ENGINE_DIR}/fsync/" and "--delete" in src_args

    pyz = tmp_path / "fsync.pyz"; pyz.write_bytes(b"PK")  # zipapp: pkg is zip member
    src_args, dst = _engine_push_args(pyz / "fsync")
    assert dst == f"{ENGINE_DIR}/fsync.pyz"
    assert src_args == [str(pyz)]

    with pytest.raises(HomesyncError):                    # neither dir nor zip
        _engine_push_args(tmp_path / "ghost" / "fsync")


def test_deploy_to_backup_failure_aborts_before_overwrite(monkeypatch):
    """Review #2: a FAILED backup cp (disk full, perms) must abort the deploy
    before anything is pushed or overwritten — never proceed without a .bak."""
    from fsync.meta_deploy import DeployError
    fake = _FakeRemote(fail=lambda c: ".bak; fi" in c)    # the backup command
    hs = _wire(monkeypatch, fake)
    with pytest.raises(DeployError) as e:
        hs._deploy_to("dev@remote", "p.pyz", "c.yaml")
    assert "backup failed" in str(e.value)
    assert fake.pushed == []                              # nothing overwritten


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


# --------------------------------------------------------------------------- #
# deploy daemon: wheelhouse + standalone fsyncd venv refresh                  #
# --------------------------------------------------------------------------- #

def test_build_wheelhouse_refuses_from_pyz(tmp_path, monkeypatch):
    import fsync.meta_deploy as md
    monkeypatch.setattr(md, "_pkg_dir", lambda: tmp_path / "zip-member" / "fsync")
    with pytest.raises(md.DeployError, match="source"):
        md.build_wheelhouse(tmp_path / "wh")


def _wire_daemon(monkeypatch, ssh_out="daemon-deploy: ok", rc=0):
    import fsync.homesync as hs
    calls = {"ssh": [], "rsync": []}

    def fake_ssh(target, cmd, **kw):
        calls["ssh"].append((target, cmd))
        return CompletedProcess([], rc, stdout=ssh_out, stderr="")

    def fake_rsync_dir(src, target, dst):
        calls["rsync"].append((src, target, dst))

    monkeypatch.setattr(hs, "_ssh_to", fake_ssh)
    monkeypatch.setattr(hs, "_rsync_dir_push", fake_rsync_dir)
    return hs, calls


def test_daemon_deploy_remote_ships_wheelhouse_then_swaps(monkeypatch):
    hs, calls = _wire_daemon(monkeypatch)
    res = hs._daemon_deploy_to("dev@b.lan", "/x/wheelhouse")
    assert res["ok"] and not res["rolled_back"]
    assert calls["rsync"] == [("/x/wheelhouse", "dev@b.lan", ".fsync/deploy/wheelhouse")]
    script = calls["ssh"][-1][1]
    # build-at-.new before swap, .bak rollback, unit reinstall from the new venv
    assert '"$V.new"' in script and '"$V.bak"' in script
    assert "daemon install" in script and "systemctl --user restart" in script


def test_daemon_deploy_remote_rollback_reported(monkeypatch):
    hs, _ = _wire_daemon(monkeypatch, ssh_out="daemon-deploy: rolled-back", rc=1)
    res = hs._daemon_deploy_to("dev@b.lan", "/x/wh")
    assert not res["ok"] and res["rolled_back"]


def test_daemon_deploy_local_runs_bash_no_ship(monkeypatch):
    import fsync.homesync as hs
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return CompletedProcess(argv, 0, stdout="daemon-deploy: ok", stderr="")

    monkeypatch.setattr(hs.subprocess, "run", fake_run)
    res = hs._daemon_deploy_to(None, "/x/wh")
    assert res["ok"] and seen["argv"][0] == "bash"


def _daemon_hosts_cfg(tmp_path, extra=""):
    cfg = tmp_path / "hosts.yaml"
    cfg.write_text(
        "requires: [git-sync]\n"
        "hosts:\n"
        "  boxA: {ssh: dev@a.lan, peer: {host: b.lan, user: dev}, defaults: {direction: push}}\n"
        "  boxB: {ssh: dev@b.lan, peer: {host: a.lan, user: dev}, defaults: {direction: pull}}\n"
        + extra
        + "shared:\n  profiles:\n    repos: {kind: git, paths: [p]}\n")
    return cfg


def test_deploy_daemon_dry_run_lists_local_and_remotes(tmp_path, monkeypatch, capsys):
    from fsync.cli import main
    monkeypatch.setattr(socket, "gethostname", lambda: "boxA")
    cfg = _daemon_hosts_cfg(tmp_path)
    assert main(["deploy", "daemon", "--config", str(cfg), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "local (this box)" in out and "boxB (dev@b.lan)" in out


def test_deploy_daemon_local_only_skips_config(tmp_path, monkeypatch, capsys):
    from fsync.cli import main
    assert main(["deploy", "daemon", "--local", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "local (this box)" in out and "boxB" not in out


def test_deploy_daemon_continues_after_one_host_fails(tmp_path, monkeypatch, capsys):
    from fsync.cli import main
    import fsync.homesync as hs
    import fsync.meta_deploy as md
    monkeypatch.setattr(socket, "gethostname", lambda: "boxA")
    cfg = _daemon_hosts_cfg(
        tmp_path, "  boxC: {ssh: dev@c.lan, peer: {host: a.lan, user: dev}}\n")
    monkeypatch.setattr(md, "build_wheelhouse",
                        lambda out_dir=None: {"path": "/x/wh", "wheels": ["fsync-0.2.0-py3-none-any.whl"]})
    attempted = []

    def fake_deploy(target, wh):
        attempted.append(target)
        if target == "dev@b.lan":
            raise md.DeployError("boom")
        return {"ok": True, "rolled_back": False, "detail": ""}

    monkeypatch.setattr(hs, "_daemon_deploy_to", fake_deploy)
    assert main(["deploy", "daemon", "--config", str(cfg)]) == 1
    # local + both remotes attempted despite boxB failing
    assert attempted == [None, "dev@b.lan", "dev@c.lan"]
