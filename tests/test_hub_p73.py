"""P7.3 hub-sync (docs/hub-sync.md R39): kind:git through the hub's bare repos,
kind:svn through the hub's snapshot store, the artifact + self-update, and the
acceptance scenario — a stale producer snapshot can no longer roll a receiver
back (the auto-apply stomp is a no-op)."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from fsync import homesync as hs
from fsync import git_repo_sync as grs
from fsync.hubstate import HubState

HOST = socket.gethostname().split(".")[0]
needs_git = pytest.mark.skipif(shutil.which("git") is None or shutil.which("rsync") is None,
                               reason="git + rsync required")


def _git(repo: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "HOME": str(repo.parent), "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


class GitFleet:
    """Producer home A (box 'boxA', repos direction both) and receiver home B
    ('boxB', pull + apply:auto) against a local hub."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp, self.mp = tmp_path, monkeypatch
        self.hub_root = tmp_path / "hub"
        self.cfgs = {}
        for name, direction in (("A", "both"), ("B", "pull")):
            cfg = tmp_path / f"cfg-{name}.yaml"
            cfg.write_text(f"""
version: 2
requires: [hub-sync, hub-stores]
hub: {{host: {HOST}, root: {self.hub_root}}}
hosts:
  {HOST}: {{role: [hub, spoke]}}
shared:
  profiles:
    repos: {{kind: git, apply: auto, direction: {direction}, paths: [workspaces]}}
""")
            self.cfgs[name] = str(cfg)
        self.homes = {n: tmp_path / f"home-{n}" for n in "AB"}
        for h in self.homes.values():
            (h / "workspaces").mkdir(parents=True)
        self.be("A")
        from fsync.hub import hub_init
        hub, profiles, _ = hs.load_config(self.cfgs["A"])
        hub_init(hub, profiles)
        self.hub = hub
        self.state = HubState(self.hub_root / "state.sqlite")

    def be(self, name: str) -> Path:
        home = self.homes[name]
        self.mp.setenv("HOME", str(home))
        self.mp.setattr(hs, "STATE_ROOT", str(home / ".local" / "state" / "fsync"))
        self.mp.setattr(hs, "_local_short_host", lambda n=name: f"box{n}")
        return home

    def run(self, name: str, expect_rc: int = 0, **kw) -> dict:
        self.be(name)
        args = dict(all=True, profile=None, dry_run=False, workers=1, notify=False, plan_only=False,
                    config=self.cfgs[name], allow_mass_delete=False)
        args.update(kw)
        rc = hs._cmd_run(argparse.Namespace(**args))
        assert rc == expect_rc, f"run on {name} rc={rc}"
        rid = (hs.state_root() / "runs" / "latest").read_text().strip()
        return json.loads((hs.state_root() / "runs" / rid / "report.json").read_text())

    def repo(self, name: str) -> Path:
        return self.homes[name] / "workspaces" / "proj"


@needs_git
def test_git_snapshot_apply_and_stale_stomp_is_noop(tmp_path, monkeypatch):
    f = GitFleet(tmp_path, monkeypatch)
    ra = f.repo("A")
    ra.mkdir()
    _git(ra, "init", "-q", "-b", "main")
    (ra / "a.txt").write_text("one")
    _git(ra, "add", "."); _git(ra, "commit", "-q", "-m", "one")
    (ra / "a.txt").write_text("two")
    _git(ra, "commit", "-q", "-am", "two")
    (ra / "wip.txt").write_text("uncommitted")          # dirty worktree travels too
    head_a = _git(ra, "rev-parse", "HEAD")

    # producer run: snapshot lands in the hub's bare repo + registry, no bundle files
    rep = f.run("A")
    repos = rep["profiles"]["repos"]
    assert repos["store"] == "hub" and repos["repos"][0]["branch"] == "main" and repos["repos"][0]["dirty"]
    bare = f.hub_root / "git" / "workspaces" / "proj.git"
    assert (bare / "HEAD").exists()
    refs = subprocess.run(["git", "-C", str(bare), "for-each-ref", "--format=%(refname)"],
                          capture_output=True, text=True).stdout.split()
    assert "refs/fsync/boxA/heads/main" in refs and "refs/fsync/boxA/wip" in refs
    assert not (f.homes["A"] / ".fsync" / "git-bundles").exists()
    regs = f.state.kind_snapshots("git", "repos")
    assert len(regs) == 1 and regs[0]["host"] == "boxA" and regs[0]["meta"]["head"] == head_a
    assert f.state.status()["kinds"]["repos"]["hosts"]["boxA"]["snapshots"] == 1

    # receiver run: repo created from the hub with HEAD, dirty file and all
    rep = f.run("B")
    rb = f.repo("B")
    applied = rep["profiles"]["repos"]
    assert applied["store"] == "hub" and len(applied["applied"]) == 1, applied
    assert _git(rb, "rev-parse", "HEAD") == head_a and (rb / "wip.txt").read_text() == "uncommitted"
    assert _git(rb, "symbolic-ref", "--short", "HEAD") == "main"
    # a second receiver run is "up to date"
    rep = f.run("B")
    assert rep["profiles"]["repos"]["applied"] == [] and \
        rep["profiles"]["repos"]["skipped"][0]["reason"] in ("up to date", "receiver has local changes")

    # the stomp scenario: the receiver commits further (ahead); the producer
    # re-snapshots its OLD state; the receiver must not be rolled back.
    (rb / "wip.txt").unlink()
    (rb / "b.txt").write_text("from B")
    _git(rb, "add", "."); _git(rb, "commit", "-q", "-m", "three on B")
    head_b = _git(rb, "rev-parse", "HEAD")
    f.run("A")                                           # unchanged A → same snapshot again
    rep = f.run("B")
    sk = rep["profiles"]["repos"]["skipped"]
    assert rep["profiles"]["repos"]["applied"] == [] and sk and "ahead" in sk[0]["reason"]
    assert _git(rb, "rev-parse", "HEAD") == head_b        # untouched
    assert rep["profiles"]["repos"]["status"] == "attention"

    # the producer catches up with B's commit (as a human would), snapshots → receiver converges
    (ra / "wip.txt").unlink()
    _git(ra, "fetch", "-q", str(rb), "main")
    _git(ra, "merge", "-q", "--ff-only", "FETCH_HEAD")
    assert _git(ra, "rev-parse", "HEAD") == head_b
    f.run("A")
    rep = f.run("B")
    assert rep["profiles"]["repos"]["applied"] == [] and rep["profiles"]["repos"]["skipped"][0]["reason"] == "up to date"
    # a new producer commit flows through
    (ra / "c.txt").write_text("c"); _git(ra, "add", "."); _git(ra, "commit", "-q", "-m", "four")
    f.run("A")
    rep = f.run("B")
    assert len(rep["profiles"]["repos"]["applied"]) == 1
    assert _git(rb, "rev-parse", "HEAD") == _git(ra, "rev-parse", "HEAD")


@needs_git
def test_git_cli_status_and_apply_through_hub(tmp_path, monkeypatch, capsys):
    f = GitFleet(tmp_path, monkeypatch)
    ra = f.repo("A"); ra.mkdir()
    _git(ra, "init", "-q", "-b", "main"); (ra / "x").write_text("x"); _git(ra, "add", "."); _git(ra, "commit", "-q", "-m", "x")
    f.be("A")
    assert hs.cmd_git(argparse.Namespace(git_cmd="snapshot", profile=None, config=f.cfgs["A"], bundle_dir=None)) == 0
    f.be("B")
    assert hs.cmd_git(argparse.Namespace(git_cmd="status", repo=None, config=f.cfgs["B"], bundle_dir=None)) == 0
    out = capsys.readouterr().out
    assert "proj: workspaces/proj [CHANGE]" in out and "(new: absent locally)" in out
    assert hs.cmd_git(argparse.Namespace(git_cmd="apply", repo=None, config=f.cfgs["B"], bundle_dir=None,
                                         backup_root=None, force=False, mirror_branches=False, keep=10)) == 0
    assert (f.repo("B") / "x").read_text() == "x"
    # a producer never applies its own snapshot: status on A has nothing to show
    f.be("A")
    assert hs.cmd_git(argparse.Namespace(git_cmd="status", repo=None, config=f.cfgs["A"], bundle_dir=None)) == 2


def test_kind_snapshot_registry_and_newest_wins(tmp_path):
    st = HubState(tmp_path / "s.sqlite")
    st.put_kind_snapshot("git", "repos", "ws/p", "minis4dx", {"head": "aaa", "name": "p"}, now=100.0)
    st.put_kind_snapshot("git", "repos", "ws/p", "fury4dx", {"head": "bbb", "name": "p"}, now=200.0)
    st.put_kind_snapshot("svn", "svn", "ws/q", "minis4dx", {"revision": 5, "name": "q"}, now=50.0)
    rows = st.kind_snapshots("git", "repos")
    assert [r["host"] for r in rows] == ["fury4dx", "minis4dx"]          # newest first per rel
    assert st.kind_snapshots("svn")[0]["meta"]["revision"] == 5
    prof = hs.Profile(name="repos", paths=["ws"], kind="git")
    from fsync.hubstate import LocalHubControl
    ctl = LocalHubControl(st, tmp_path)
    metas = hs._hub_kind_metas(ctl, "git", prof, self_host="omen4dx")
    assert len(metas) == 1 and metas[0]["host"] == "fury4dx" and metas[0]["head"] == "bbb"
    assert hs._hub_kind_metas(ctl, "git", prof, self_host="fury4dx")[0]["host"] == "minis4dx"
    st.put_kind_snapshot("git", "repos", "ws/p", "minis4dx", {"head": "ccc", "name": "p"}, now=300.0)  # upsert
    assert hs._hub_kind_metas(ctl, "git", prof, self_host="omen4dx")[0]["head"] == "ccc"


def test_artifact_publish_route_and_install(tmp_path, monkeypatch):
    from fsync.hub import install_artifact, publish_artifact, _version_tuple
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    hub = hs.Hub(host=HOST, local=True, root=str(tmp_path / "hub"), roles=["hub"])
    pyz = tmp_path / "fsync.pyz"; pyz.write_bytes(b"PK-fake-pyz")
    cfg = tmp_path / "c.yaml"; cfg.write_text("version: 2\n")
    man = publish_artifact(hub, pyz, cfg)
    art = tmp_path / "hub" / "artifact"
    assert (art / "fsync.pyz").read_bytes() == b"PK-fake-pyz" and json.loads((art / "manifest.json").read_text())["version"] == man["version"]
    # install into a home: backup-first, sha-verified
    home = tmp_path / "spoke"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "fsync").write_bytes(b"old")
    res = install_artifact(art / "fsync.pyz", art / "sync-profiles.yaml", man, home=home, verify=False)
    assert (home / ".local" / "bin" / "fsync").read_bytes() == b"PK-fake-pyz"
    assert (home / ".local" / "bin" / "fsync.bak").read_bytes() == b"old"
    assert (home / ".config" / "fsync" / "sync-profiles.yaml").read_text() == "version: 2\n"
    bad = dict(man); bad["pyz"] = {**man["pyz"], "sha256": "0" * 64}
    with pytest.raises(hs.HomesyncError, match="sha256"):
        install_artifact(art / "fsync.pyz", art / "sync-profiles.yaml", bad, home=home, verify=False)
    assert _version_tuple("0.10.0") > _version_tuple("0.9.0")
    # daemon routes
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from fsync.daemon import create_app
    cfg2 = tmp_path / "hubcfg.yaml"
    cfg2.write_text(f"""
version: 2
hub: {{host: {HOST}, root: {tmp_path / 'hub'}}}
hosts: {{{HOST}: {{role: [hub]}}}}
shared: {{profiles: {{docs: {{paths: [D]}}}}}}
""")
    c = TestClient(create_app(str(cfg2), require_token=False))
    assert c.get("/v1/hub/artifact").json()["pyz"]["sha256"] == man["pyz"]["sha256"]
    assert c.get("/v1/hub/artifact/pyz").content == b"PK-fake-pyz"
    assert c.get("/v1/hub/artifact/nope").status_code == 404
    r = c.post("/v1/hub/kind/git/snapshot", json={"profile": "repos", "rel": "ws/p", "host": "fury4dx",
                                                 "meta": {"head": "abc"}})
    assert r.status_code == 200
    assert c.get("/v1/hub/kind/git/snapshots", params={"profile": "repos"}).json()[0]["meta"]["head"] == "abc"
    assert c.post("/v1/hub/kind/hg/snapshot", json={"profile": "x", "rel": "y", "host": "h", "meta": {}}).status_code == 400


def test_features_and_version():
    from fsync import FEATURES, __version__
    assert "hub-stores" in FEATURES and __version__ == "0.10.0"


def test_drop_retired_profile(tmp_path):
    st = HubState(tmp_path / "s.sqlite")
    st.index_tree("old", [{"path": "a", "size": 1, "mtime": 1.0, "hash": "h"}], now=1.0)
    st.record_run("fury4dx", "old", "R1", {"status": "ok"})
    st.put_kind_snapshot("git", "old", "r", "minis4dx", {})
    st.index_tree("keep", [{"path": "b", "size": 1, "mtime": 1.0, "hash": "h"}], now=1.0)
    res = st.drop_profile("old")
    assert res["paths"] == 1 and res["journal"] == 1 and res["spoke_runs"] == 1 and res["kind_snapshots"] == 1
    assert not st.is_indexed("old") and st.is_indexed("keep")
    assert "old" not in st.status()["profiles"] and "keep" in st.status()["profiles"]
