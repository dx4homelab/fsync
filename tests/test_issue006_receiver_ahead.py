"""ISSUE-006 sub-finding: a stale bundle must never move a receiver backwards.

The incident (2026-08-27, refactor4homelab on fury): auto-apply enforced a bundle
whose head was an ancestor of the receiver's HEAD, silently rolling fresh local
commits off the branch. These tests pin both guard layers: the pre-fetch skip in
``_auto_apply_decision`` / manual-apply path, and the authoritative post-fetch
refusal inside ``apply_repo`` (which also catches true divergence, where the
incoming head is unknown to the receiver until fetched).
"""

from __future__ import annotations

import subprocess

import pytest

from fsync import git_repo_sync as grs
from fsync import homesync as hs
from fsync.homesync import Profile, _auto_apply_decision, auto_apply_git_profile


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


def _head(repo):
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                       check=True, capture_output=True, text=True)
    return r.stdout.strip()


def _commit(repo, fname, content, msg):
    (repo / fname).write_text(content)
    _git(repo, "add", fname)
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-qm", msg)


def _stale_bundle_receiver(tmp_path, monkeypatch):
    """Producer snapshot at C1; receiver applies it, then commits C2 locally.
    The staged bundle is now stale relative to the receiver."""
    prod = _init_repo(tmp_path / "producer/proj")
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    grs.snapshot_repo(prod, bundle_dir, name="proj", rel="workspaces/primary/proj")
    recv_home = tmp_path / "recv"
    monkeypatch.setattr(hs, "_home", lambda: recv_home)
    monkeypatch.setattr(hs, "DEFAULT_GIT_BACKUP_ROOT", str(tmp_path / "backups"))
    prof = Profile(name="repos", paths=["workspaces/primary"], kind="git",
                   apply="auto", direction="pull")
    auto_apply_git_profile(prof, bundle_dir, run_id="t1", dry_run=False, log=lambda *a: None)
    target = recv_home / "workspaces/primary/proj"
    _commit(target, "f.txt", "v2 local\n", "receiver-authored work")
    return prod, bundle_dir, prof, target


# --------------------------------------------------------------------------- #
# check_receiver_ahead (pre-fetch heuristic)                                   #
# --------------------------------------------------------------------------- #

def test_detects_stale_bundle_rollback(tmp_path, monkeypatch):
    _, bundle_dir, _, target = _stale_bundle_receiver(tmp_path, monkeypatch)
    meta = grs.read_meta(bundle_dir, "proj")
    info = grs.check_receiver_ahead(target, meta)
    assert info is not None
    assert info["receiver_head"] == _head(target)
    assert info["incoming_head"] == meta["head"]


def test_none_when_up_to_date(tmp_path, monkeypatch):
    prod, bundle_dir, _, target = _stale_bundle_receiver(tmp_path, monkeypatch)
    _git(target, "reset", "-q", "--hard", "HEAD~1")  # back to the applied C1
    meta = grs.read_meta(bundle_dir, "proj")
    assert grs.check_receiver_ahead(target, meta) is None


def test_none_when_receiver_behind(tmp_path, monkeypatch):
    prod, bundle_dir, _, target = _stale_bundle_receiver(tmp_path, monkeypatch)
    _git(target, "reset", "-q", "--hard", "HEAD~1")
    _commit(prod, "f.txt", "v2 producer\n", "producer moves on")
    grs.snapshot_repo(prod, bundle_dir, name="proj", rel="workspaces/primary/proj")
    meta = grs.read_meta(bundle_dir, "proj")
    # incoming head unknown to the receiver -> heuristic stays silent; the apply
    # is a legitimate fast-forward
    assert grs.check_receiver_ahead(target, meta) is None


def test_none_for_absent_repo(tmp_path):
    assert grs.check_receiver_ahead(tmp_path / "nope", {"head": "x"}) is None


# --------------------------------------------------------------------------- #
# guard layers: decision skip + auto-apply run + apply_repo refusal            #
# --------------------------------------------------------------------------- #

def test_decision_skips_receiver_ahead(tmp_path, monkeypatch):
    _, bundle_dir, _, target = _stale_bundle_receiver(tmp_path, monkeypatch)
    meta = grs.read_meta(bundle_dir, "proj")
    ok, reason = _auto_apply_decision(target, meta)
    assert not ok
    assert "receiver is ahead" in reason and "re-snapshot" in reason


def test_autoapply_run_preserves_local_commit(tmp_path, monkeypatch):
    _, bundle_dir, prof, target = _stale_bundle_receiver(tmp_path, monkeypatch)
    before = _head(target)
    res = auto_apply_git_profile(prof, bundle_dir, run_id="t2", dry_run=False, log=lambda *a: None)
    assert res["applied"] == []
    assert any("receiver is ahead" in s["reason"] for s in res["skipped"])
    assert _head(target) == before
    assert (target / "f.txt").read_text() == "v2 local\n"


def test_apply_repo_refuses_rollback(tmp_path, monkeypatch):
    _, bundle_dir, _, target = _stale_bundle_receiver(tmp_path, monkeypatch)
    meta = grs.read_meta(bundle_dir, "proj")
    before = _head(target)
    with pytest.raises(grs.GitSyncError, match="refusing to roll back"):
        grs.apply_repo(target, bundle_dir / "proj.bundle", meta,
                       backup_dir=tmp_path / "backups/manual")
    assert _head(target) == before


def test_apply_repo_force_overrides(tmp_path, monkeypatch):
    _, bundle_dir, _, target = _stale_bundle_receiver(tmp_path, monkeypatch)
    meta = grs.read_meta(bundle_dir, "proj")
    res = grs.apply_repo(target, bundle_dir / "proj.bundle", meta,
                         backup_dir=tmp_path / "backups/manual", force=True)
    assert res["applied"] and _head(target) == meta["head"]
    assert res["backup"]  # R15: the rolled-back state is recoverable


def test_apply_repo_refuses_true_divergence(tmp_path, monkeypatch):
    # Same branch, both sides committed independently: the pre-fetch heuristic
    # is blind (incoming head unknown here), so the post-fetch net must catch it.
    prod, bundle_dir, prof, target = _stale_bundle_receiver(tmp_path, monkeypatch)
    _commit(prod, "g.txt", "producer side\n", "producer diverges")
    grs.snapshot_repo(prod, bundle_dir, name="proj", rel="workspaces/primary/proj")
    meta = grs.read_meta(bundle_dir, "proj")
    assert grs.check_receiver_ahead(target, meta) is None  # heuristic blind
    before = _head(target)
    with pytest.raises(grs.GitSyncError, match="refusing to roll back"):
        grs.apply_repo(target, bundle_dir / "proj.bundle", meta,
                       backup_dir=tmp_path / "backups/manual")
    assert _head(target) == before


def test_autoapply_still_fast_forwards_receiver_behind(tmp_path, monkeypatch):
    prod, bundle_dir, prof, target = _stale_bundle_receiver(tmp_path, monkeypatch)
    _git(target, "reset", "-q", "--hard", "HEAD~1")
    _commit(prod, "f.txt", "v3 producer\n", "producer advances")
    grs.snapshot_repo(prod, bundle_dir, name="proj", rel="workspaces/primary/proj")
    res = auto_apply_git_profile(prof, bundle_dir, run_id="t3", dry_run=False, log=lambda *a: None)
    assert [a["repo"] for a in res["applied"]] == ["proj"]
    assert (target / "f.txt").read_text() == "v3 producer\n"
