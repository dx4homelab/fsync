"""ISSUE-008: a refused apply must never poison the next fetch.

The incident (fury, 2026-09-08 → 09-13): the ISSUE-006 post-fetch guard refused
to roll ``asap__owasp-coraza-proxy`` back, correctly — but it raised after the
bundle had been fetched into ``refs/fsync/incoming/*`` and before that scratch
namespace was tidied. The next snapshot's WIP commit is never a descendant of
the stale ``incoming/wip``, and with unforced refspecs git rejected that one ref
as non-fast-forward and failed the whole fetch. Every hourly run for five days
then reported ``git fetch … failed`` — hiding the real, actionable refusal.

Also pins ISSUE-006 fixes B + C: that refusal is a standing condition a human
resolves, so auto-apply records it as a *sticky skip* (profile ``status:
attention``, escalating to a warning after N consecutive runs) rather than an
error that fails — and notifies — every run.
"""

from __future__ import annotations

import json
import subprocess

from fsync import git_repo_sync as grs
from fsync import homesync as hs
from fsync.homesync import Profile, auto_apply_git_profile


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _init_repo(path, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "checkout", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@test")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("v1\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-qm", "init")
    return path


def _commit(repo, fname, content, msg):
    (repo / fname).write_text(content)
    _git(repo, "add", fname)
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-qm", msg)


def _incoming_refs(repo):
    return _git(repo, "for-each-ref", "--format=%(refname)", f"{grs.INCOMING}/").splitlines()


def _diverged(tmp_path, monkeypatch):
    """Producer at C1 → receiver applies it and commits C2 locally → producer
    commits C1' on the same branch: true divergence, only the post-fetch guard
    can see it."""
    prod = _init_repo(tmp_path / "producer/proj")
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    grs.snapshot_repo(prod, bundle_dir, name="proj", rel="workspaces/primary/proj")
    recv_home = tmp_path / "recv"
    monkeypatch.setattr(hs, "_home", lambda: recv_home)
    monkeypatch.setattr(hs, "DEFAULT_GIT_BACKUP_ROOT", str(tmp_path / "backups"))
    prof = Profile(name="repos", paths=["workspaces/primary"], kind="git",
                   apply="auto", direction="pull")
    auto_apply_git_profile(prof, bundle_dir, run_id="r0", dry_run=False, log=lambda *a: None)
    target = recv_home / "workspaces/primary/proj"
    _commit(target, "f.txt", "v2 receiver\n", "receiver-authored work")
    _commit(prod, "g.txt", "producer side\n", "producer diverges")
    (prod / "wip.txt").write_text("dirty 1\n")           # WIP commit content
    grs.snapshot_repo(prod, bundle_dir, name="proj", rel="workspaces/primary/proj")
    return prod, bundle_dir, prof, target


def test_refusal_leaves_no_incoming_refs_and_next_fetch_succeeds(tmp_path, monkeypatch):
    prod, bundle_dir, prof, target = _diverged(tmp_path, monkeypatch)
    before = _git(target, "rev-parse", "HEAD")
    lines: list[str] = []
    res = auto_apply_git_profile(prof, bundle_dir, run_id="r1", dry_run=False, log=lines.append)
    assert res["errors"] == []                                   # not an error any more
    assert [s["repo"] for s in res["skipped"]] == ["proj"]
    assert "receiver has commits the incoming bundle lacks" in res["skipped"][0]["reason"]
    assert res["status"] == "attention"
    assert _git(target, "rev-parse", "HEAD") == before
    assert _incoming_refs(target) == []                          # the ISSUE-008 fix

    # The producer moves on again: new commit AND a new WIP commit. Before the
    # fix, fetching this bundle failed non-fast-forward on incoming/wip.
    _commit(prod, "h.txt", "more\n", "producer again")
    (prod / "wip.txt").write_text("dirty 2\n")
    grs.snapshot_repo(prod, bundle_dir, name="proj", rel="workspaces/primary/proj")
    res = auto_apply_git_profile(prof, bundle_dir, run_id="r2", dry_run=False, log=lines.append)
    assert res["errors"] == []
    assert not any("git fetch" in ln for ln in lines)
    assert "receiver has commits the incoming bundle lacks" in res["skipped"][0]["reason"]
    assert _git(target, "rev-parse", "HEAD") == before


def test_fetch_incoming_overrides_stale_scratch_refs(tmp_path, monkeypatch):
    # Even a planted, unrelated incoming/wip (the exact leftover from the
    # incident) cannot gate the fetch: the namespace is pruned and forced.
    prod, bundle_dir, prof, target = _diverged(tmp_path, monkeypatch)
    _git(target, "update-ref", f"{grs.INCOMING}/wip", _git(target, "rev-parse", "HEAD"))
    _git(target, "update-ref", f"{grs.INCOMING}/heads/main", _git(target, "rev-parse", "HEAD"))
    grs._fetch_incoming(target, bundle_dir / "proj.bundle")
    meta = grs.read_meta(bundle_dir, "proj")
    assert _git(target, "rev-parse", f"{grs.INCOMING}/heads/main") == meta["head"]
    assert _git(target, "rev-parse", f"{grs.INCOMING}/wip") == meta["cwork"]


def test_restore_backup_survives_stale_incoming_refs(tmp_path, monkeypatch):
    prod = _init_repo(tmp_path / "producer/proj")
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    grs.snapshot_repo(prod, bundle_dir, name="proj", rel="workspaces/primary/proj")
    recv = _init_repo(tmp_path / "recv/proj")                   # own history
    _commit(recv, "mine.txt", "receiver\n", "receiver work")
    meta = grs.read_meta(bundle_dir, "proj")
    backup_dir = tmp_path / "backup"
    grs.apply_repo(recv, bundle_dir / "proj.bundle", meta, backup_dir=backup_dir, force=True)
    assert _git(recv, "rev-parse", "HEAD") == meta["head"]
    _git(recv, "update-ref", f"{grs.INCOMING}/wip", meta["head"])    # plant a leftover
    grs.restore_backup(recv, backup_dir, "proj")
    assert (recv / "mine.txt").read_text() == "receiver\n"
    assert _incoming_refs(recv) == []


def test_sticky_skip_escalates_after_n_runs_and_clears_when_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "AUTO_APPLY_STICKY_RUNS", 3)
    monkeypatch.setattr(hs, "AUTO_APPLY_REMIND_EVERY", 2)
    prod, bundle_dir, prof, target = _diverged(tmp_path, monkeypatch)
    warned = []
    for i in range(1, 7):
        res = auto_apply_git_profile(prof, bundle_dir, run_id=f"r{i}", dry_run=False,
                                     log=lambda *a: None)
        assert res["status"] == "attention"
        if res["warnings"]:
            warned.append(i)
            assert "runs in a row since r1" in res["warnings"][0]
    assert warned == [3, 5]                                      # threshold, then reminders
    st = json.loads((hs.state_root() / "auto-apply-sticky.json").read_text())
    assert st["repos/proj"]["runs"] == 6 and st["repos/proj"]["first_run"] == "r1"

    # A dry run reads but never advances the streak.
    res = auto_apply_git_profile(prof, bundle_dir, run_id="dry", dry_run=True, log=lambda *a: None)
    assert json.loads((hs.state_root() / "auto-apply-sticky.json").read_text())["repos/proj"]["runs"] == 6

    # Human resolution: the receiver drops its own commit → plain fast-forward
    # → applied, status ok, streak gone.
    _git(target, "reset", "-q", "--hard", "HEAD~1")
    res = auto_apply_git_profile(prof, bundle_dir, run_id="r7", dry_run=False, log=lambda *a: None)
    assert [a["repo"] for a in res["applied"]] == ["proj"]
    assert res["status"] == "ok"
    assert "repos/proj" not in json.loads((hs.state_root() / "auto-apply-sticky.json").read_text())


def test_sticky_streak_survives_producer_commits(tmp_path, monkeypatch):
    # The reason text carries commit ids that change as the producer commits;
    # the streak must key on the reason shape, not the ids.
    monkeypatch.setattr(hs, "AUTO_APPLY_STICKY_RUNS", 2)
    prod, bundle_dir, prof, target = _diverged(tmp_path, monkeypatch)
    auto_apply_git_profile(prof, bundle_dir, run_id="r1", dry_run=False, log=lambda *a: None)
    _commit(prod, "k.txt", "x\n", "producer commits again")
    grs.snapshot_repo(prod, bundle_dir, name="proj", rel="workspaces/primary/proj")
    res = auto_apply_git_profile(prof, bundle_dir, run_id="r2", dry_run=False, log=lambda *a: None)
    assert res["warnings"] and "2 runs in a row" in res["warnings"][0]
