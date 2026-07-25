"""ISSUE-001 durable fix (C): file-profile exclude injection + guarded receiver
auto-apply (apply: auto)."""

from __future__ import annotations

import subprocess

import pytest

from fsync import git_repo_sync as grs
from fsync import homesync as hs
from fsync.homesync import (
    Profile, git_repo_excludes_for_file_profiles, auto_apply_git_repos,
    _auto_apply_decision, auto_apply_git_profile,
)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(path, branch="main", commit=True):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "checkout", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@test")
    _git(path, "config", "user.name", "t")
    if commit:
        (path / "f.txt").write_text("v1\n")
        _git(path, "add", "f.txt")
        _git(path, "commit", "-qm", "init")
    return path


# --------------------------------------------------------------------------- #
# exclude injection                                                            #
# --------------------------------------------------------------------------- #

def test_exclude_injection_targets_only_auto_git_repos(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "_home", lambda: tmp_path)
    _init_repo(tmp_path / "workspaces/primary/repoA")
    _init_repo(tmp_path / "workspaces/primary/sub/repoB")
    (tmp_path / "Documents").mkdir()

    git_prof = Profile(name="repos", paths=["workspaces/primary"], kind="git",
                       apply="auto", direction="pull")
    file_prof = Profile(name="primary", paths=["workspaces/primary"], kind="files")
    docs = Profile(name="documents", paths=["Documents"], kind="files")
    profiles = {"repos": git_prof, "primary": file_prof, "documents": docs}

    out = git_repo_excludes_for_file_profiles(profiles)
    assert out["primary"] == ["repoA/*", "sub/repoB/*"]
    assert "documents" not in out  # unrelated file profile untouched


def test_exclude_injection_noop_when_on_demand(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "_home", lambda: tmp_path)
    _init_repo(tmp_path / "workspaces/primary/repoA")
    git_prof = Profile(name="repos", paths=["workspaces/primary"], kind="git",
                       apply="on-demand", direction="pull")
    file_prof = Profile(name="primary", paths=["workspaces/primary"], kind="files")
    assert git_repo_excludes_for_file_profiles({"repos": git_prof, "primary": file_prof}) == {}


def test_auto_apply_git_repos_only_pull_auto():
    assert auto_apply_git_repos(Profile("r", ["x"], kind="git", apply="auto", direction="pull"))
    assert not auto_apply_git_repos(Profile("r", ["x"], kind="git", apply="auto", direction="push"))
    assert not auto_apply_git_repos(Profile("r", ["x"], kind="git", apply="on-demand", direction="pull"))
    assert not auto_apply_git_repos(Profile("r", ["x"], kind="files", direction="pull"))


# --------------------------------------------------------------------------- #
# guard decision                                                               #
# --------------------------------------------------------------------------- #

def test_decision_new_repo_applies(tmp_path):
    ok, reason = _auto_apply_decision(tmp_path / "absent", {"branch": "main", "head": "x"})
    assert ok and reason == "new"


def test_decision_clean_same_branch_producer_ahead_applies(tmp_path):
    # clean receiver, same branch, but producer at a different head -> apply
    repo = _init_repo(tmp_path / "r", branch="main")
    ok, reason = _auto_apply_decision(repo, {"branch": "main", "head": "0" * 40, "dirty": False})
    assert ok and reason == "clean"


def test_decision_up_to_date_skips(tmp_path):
    # clean receiver already at producer HEAD, producer not dirty -> no re-apply
    repo = _init_repo(tmp_path / "r", branch="main")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    ok, reason = _auto_apply_decision(repo, {"branch": "main", "head": head, "dirty": False})
    assert not ok and reason == "up to date"


def test_decision_dirty_skips(tmp_path):
    repo = _init_repo(tmp_path / "r", branch="main")
    (repo / "f.txt").write_text("dirtied\n")
    ok, reason = _auto_apply_decision(repo, {"branch": "main", "head": "x"})
    assert not ok and "local changes" in reason


def test_decision_divergent_branch_skips(tmp_path):
    repo = _init_repo(tmp_path / "r", branch="main")
    ok, reason = _auto_apply_decision(repo, {"branch": "refactor-v4", "head": "x"})
    assert not ok and reason.startswith("branch divergence")


# --------------------------------------------------------------------------- #
# auto_apply_git_profile end-to-end (real bundle)                              #
# --------------------------------------------------------------------------- #

def _bundle_producer(tmp_path):
    """Snapshot a producer repo into a bundle dir; return (bundle_dir, rel, name)."""
    prod = _init_repo(tmp_path / "producer" / "proj", branch="main")
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    grs.snapshot_repo(prod, bundle_dir, name="proj", rel="workspaces/primary/proj")
    return bundle_dir, "workspaces/primary/proj", "proj"


def test_autoapply_creates_absent_repo(tmp_path, monkeypatch):
    bundle_dir, rel, name = _bundle_producer(tmp_path)
    recv_home = tmp_path / "recv"
    monkeypatch.setattr(hs, "_home", lambda: recv_home)
    monkeypatch.setattr(hs, "DEFAULT_GIT_BACKUP_ROOT", str(tmp_path / "backups"))

    prof = Profile(name="repos", paths=["workspaces/primary"], kind="git",
                   apply="auto", direction="pull")
    res = auto_apply_git_profile(prof, bundle_dir, run_id="t", dry_run=False, log=lambda *a: None)
    assert [a["repo"] for a in res["applied"]] == ["proj"]
    target = recv_home / rel
    assert grs.is_git_repo(target) and (target / "f.txt").read_text() == "v1\n"


def test_autoapply_skips_dirty_receiver(tmp_path, monkeypatch):
    bundle_dir, rel, name = _bundle_producer(tmp_path)
    recv_home = tmp_path / "recv"
    monkeypatch.setattr(hs, "_home", lambda: recv_home)
    monkeypatch.setattr(hs, "DEFAULT_GIT_BACKUP_ROOT", str(tmp_path / "backups"))
    prof = Profile(name="repos", paths=["workspaces/primary"], kind="git",
                   apply="auto", direction="pull")
    # first apply creates it, then dirty it
    auto_apply_git_profile(prof, bundle_dir, run_id="t1", dry_run=False, log=lambda *a: None)
    (recv_home / rel / "f.txt").write_text("local edit\n")
    res = auto_apply_git_profile(prof, bundle_dir, run_id="t2", dry_run=False, log=lambda *a: None)
    assert res["applied"] == []
    assert any("local changes" in s["reason"] for s in res["skipped"])


def test_autoapply_skips_when_up_to_date(tmp_path, monkeypatch):
    bundle_dir, rel, name = _bundle_producer(tmp_path)
    recv_home = tmp_path / "recv"
    monkeypatch.setattr(hs, "_home", lambda: recv_home)
    monkeypatch.setattr(hs, "DEFAULT_GIT_BACKUP_ROOT", str(tmp_path / "backups"))
    prof = Profile(name="repos", paths=["workspaces/primary"], kind="git",
                   apply="auto", direction="pull")
    auto_apply_git_profile(prof, bundle_dir, run_id="t1", dry_run=False, log=lambda *a: None)
    # second run: receiver already at producer HEAD, clean -> no re-apply, no churn
    res = auto_apply_git_profile(prof, bundle_dir, run_id="t2", dry_run=False, log=lambda *a: None)
    assert res["applied"] == []
    assert any(s["reason"] == "up to date" for s in res["skipped"])


def test_autoapply_dry_run_mutates_nothing(tmp_path, monkeypatch):
    bundle_dir, rel, name = _bundle_producer(tmp_path)
    recv_home = tmp_path / "recv"
    monkeypatch.setattr(hs, "_home", lambda: recv_home)
    prof = Profile(name="repos", paths=["workspaces/primary"], kind="git",
                   apply="auto", direction="pull")
    res = auto_apply_git_profile(prof, bundle_dir, run_id="t", dry_run=True, log=lambda *a: None)
    assert res["applied"] and res["applied"][0].get("dry_run") is True
    assert not (recv_home / rel).exists()  # nothing created
