"""ISSUE-001 guards: the branch-divergence apply guard (Fix B) and the
file-sync untracked-introduction probe (Fix A). Both use a real temp git repo."""

from __future__ import annotations

import subprocess

import pytest

from fsync import git_repo_sync as grs
from fsync import homesync as hs


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(path, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "checkout", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@test")
    _git(path, "config", "user.name", "t")
    (path / "tracked.txt").write_text("hello\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "init")
    return path


# --------------------------------------------------------------------------- #
# Fix B: check_branch_divergence                                               #
# --------------------------------------------------------------------------- #

def test_divergence_none_when_same_branch(tmp_path):
    repo = _make_repo(tmp_path / "r", branch="main")
    assert grs.check_branch_divergence(repo, {"branch": "main", "head": "x"}) is None


def test_divergence_flagged_when_branches_differ(tmp_path):
    repo = _make_repo(tmp_path / "r", branch="main")
    div = grs.check_branch_divergence(repo, {"branch": "refactor-v4", "head": "deadbeef"})
    assert div is not None
    assert div["receiver_branch"] == "main"
    assert div["producer_branch"] == "refactor-v4"
    assert div["producer_head"] == "deadbeef"
    assert len(div["receiver_head"]) == 40  # real sha of the local commit


def test_divergence_none_for_non_repo(tmp_path):
    (tmp_path / "plain").mkdir()
    assert grs.check_branch_divergence(tmp_path / "plain", {"branch": "main"}) is None


def test_divergence_detached_receiver_flags(tmp_path):
    repo = _make_repo(tmp_path / "r", branch="main")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    _git(repo, "checkout", "-q", head)  # detach
    div = grs.check_branch_divergence(repo, {"branch": "main", "head": head})
    assert div is not None and div["receiver_branch"] is None  # detached != "main"


# --------------------------------------------------------------------------- #
# Fix A: the ghost probe + the homesync scan wrapper                           #
# --------------------------------------------------------------------------- #

def _run_probe(root, rel_paths):
    proc = subprocess.run(["python3", "-c", grs.GHOST_PROBE, str(root), *rel_paths],
                          capture_output=True, text=True)
    return [l for l in proc.stdout.splitlines() if l.strip()]


def test_probe_reports_only_untracked_in_repo(tmp_path):
    repo = _make_repo(tmp_path / "proj", branch="main")
    (repo / "ghost.whl").write_text("stray\n")           # untracked -> ghost
    (repo / "tracked.txt").write_text("changed\n")       # tracked, modified -> NOT a ghost
    out = _run_probe(tmp_path, ["proj/ghost.whl", "proj/tracked.txt"])
    assert out == ["proj/ghost.whl"]


def test_probe_ignores_files_outside_any_repo(tmp_path):
    (tmp_path / "loose.txt").write_text("x\n")           # not under a git repo
    assert _run_probe(tmp_path, ["loose.txt"]) == []


def test_probe_empty_when_no_paths(tmp_path):
    assert _run_probe(tmp_path, []) == []


def test_scan_wrapper_local_finds_ghost(tmp_path):
    repo = _make_repo(tmp_path / "proj", branch="main")
    (repo / "ghost.whl").write_text("stray\n")
    ghosts, truncated = hs._scan_untracked_introductions(
        str(tmp_path), ["proj/ghost.whl", "proj/tracked.txt"], peer=None)
    assert ghosts == ["proj/ghost.whl"]
    assert truncated is False


def test_scan_wrapper_truncation_flag(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "proj", branch="main")
    (repo / "a.whl").write_text("1\n")
    (repo / "b.whl").write_text("2\n")
    monkeypatch.setattr(hs, "MAX_GHOST_SCAN", 1)
    ghosts, truncated = hs._scan_untracked_introductions(
        str(tmp_path), ["proj/a.whl", "proj/b.whl"], peer=None)
    assert truncated is True
    assert len(ghosts) <= 1  # only the first was scanned


def test_scan_wrapper_empty_paths_noops(tmp_path):
    assert hs._scan_untracked_introductions(str(tmp_path), [], peer=None) == ([], False)
