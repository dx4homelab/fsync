"""P5 git-repo-sync: full-fidelity round-trip + safety tests.

The core guarantee (R13): snapshot -> bundle -> apply reproduces the producer's
EXACT `git status` (staged / unstaged / untracked / deletions), branch set,
HEAD, and worktree content on the receiver. Plus R15: every apply leaves a
recoverable pre-apply backup of the receiver's own work.
"""

from __future__ import annotations

import hashlib
import os
import subprocess

import pytest

from fsync import git_repo_sync as grs

_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
}


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)


def git(repo, *args, check=True):
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, env={**os.environ, **_ENV})
    if check and p.returncode != 0:
        raise AssertionError(f"git {args} failed: {p.stderr or p.stdout}")
    return p.stdout.strip()


def status(repo):
    return sorted(git(repo, "status", "--porcelain").splitlines())


def worktree_map(repo):
    """{relpath: sha1(content)} for every non-.git file — tracked or not."""
    out = {}
    root = str(repo)
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as fh:
                out[rel] = hashlib.sha1(fh.read()).hexdigest()
    return out


def make_base_repo(path, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    (path / "a.txt").write_text("a-v1\n")
    (path / "b.txt").write_text("b-v1\n")
    (path / "gone_staged.txt").write_text("del-staged\n")
    (path / "gone_unstaged.txt").write_text("del-unstaged\n")
    (path / "sub").mkdir()
    (path / "sub" / "c.txt").write_text("nested\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "c1")


def make_mixed_state(path):
    (path / "a.txt").write_text("a-STAGED\n"); git(path, "add", "a.txt")
    (path / "b.txt").write_text("b-UNSTAGED\n")
    (path / "added_staged.txt").write_text("new-STAGED\n"); git(path, "add", "added_staged.txt")
    (path / "untracked.txt").write_text("untr\n")
    (path / "untr_dir").mkdir()
    (path / "untr_dir" / "d.txt").write_text("x\n")
    git(path, "rm", "-q", "gone_staged.txt")
    (path / "gone_unstaged.txt").unlink()


def clone(src, dst):
    subprocess.run(["git", "clone", "-q", str(src), str(dst)],
                   check=True, capture_output=True, env={**os.environ, **_ENV})


# --------------------------------------------------------------------------- #

def test_roundtrip_mixed_state(tmp_path):
    prod = tmp_path / "producer"
    make_base_repo(prod)
    git(prod, "branch", "feature-x")           # extra branch to check fidelity
    make_mixed_state(prod)
    target_status = status(prod)
    target_tree = worktree_map(prod)

    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")

    recv = tmp_path / "receiver"
    clone(prod, recv)                          # older clone at c1 (committed only)
    (recv / "a.txt").write_text("a-RECEIVER-LOCAL\n")     # local edit to be overwritten
    (recv / "receiver_only.txt").write_text("stray\n")   # stray untracked to vanish

    res = grs.apply_repo(recv, meta["bundle"], meta,
                         backup_dir=tmp_path / "backups")

    assert status(recv) == target_status
    assert git(recv, "write-tree") == git(prod, "write-tree")     # index identity
    assert git(recv, "rev-parse", "HEAD") == git(prod, "rev-parse", "HEAD")
    assert git(recv, "symbolic-ref", "--short", "HEAD") == "main"
    assert worktree_map(recv) == target_tree                      # incl untracked
    # branch set + tips identical
    assert git(recv, "for-each-ref", "--format=%(refname:short) %(objectname)",
               "refs/heads/") == git(prod, "for-each-ref",
               "--format=%(refname:short) %(objectname)", "refs/heads/")
    # R15 backup exists and preserves the receiver's overwritten work
    assert res["backup"] and os.path.exists(res["backup"])


def test_backup_restores_receiver_work(tmp_path):
    prod = tmp_path / "p"; make_base_repo(prod); make_mixed_state(prod)
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")

    recv = tmp_path / "r"; clone(prod, recv)
    (recv / "a.txt").write_text("PRECIOUS-RECEIVER-WORK\n")
    res = grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk")

    # restore the receiver's pre-apply state from the backup bundle
    verify = tmp_path / "verify"; verify.mkdir()
    git(verify, "init", "-q")
    git(verify, "fetch", res["backup"], f"{grs.WIP_REF}:refs/heads/restored")
    blob = git(verify, "cat-file", "-p", "refs/heads/restored^{tree}:a.txt")
    assert blob == "PRECIOUS-RECEIVER-WORK"


def test_clean_repo_roundtrip(tmp_path):
    prod = tmp_path / "p"; make_base_repo(prod)          # clean worktree
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")
    assert meta["dirty"] is False

    recv = tmp_path / "r"; clone(prod, recv)
    (recv / "b.txt").write_text("dirty-on-receiver\n")   # receiver diverged
    grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk")
    assert status(recv) == []                            # clean, matches producer
    assert worktree_map(recv) == worktree_map(prod)


def test_untracked_strays_are_removed(tmp_path):
    prod = tmp_path / "p"; make_base_repo(prod)
    (prod / "keep_untracked.txt").write_text("keep\n")   # producer untracked -> must survive
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")

    recv = tmp_path / "r"; clone(prod, recv)
    (recv / "stray.txt").write_text("stray\n")
    (recv / "stray_dir").mkdir(); (recv / "stray_dir" / "z.txt").write_text("z\n")
    grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk")

    assert (recv / "keep_untracked.txt").exists()
    assert not (recv / "stray.txt").exists()
    assert not (recv / "stray_dir").exists()
    assert status(recv) == status(prod)


def test_detached_head(tmp_path):
    prod = tmp_path / "p"; make_base_repo(prod)
    (prod / "d.txt").write_text("more\n"); git(prod, "add", "-A"); git(prod, "commit", "-qm", "c2")
    first = git(prod, "rev-parse", "HEAD~1")
    git(prod, "checkout", "-q", "--detach", first)
    (prod / "b.txt").write_text("detached-edit\n")       # dirty while detached
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")
    assert meta["branch"] is None

    recv = tmp_path / "r"; clone(prod, recv)
    grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk")
    assert git(recv, "rev-parse", "HEAD") == first
    # HEAD is detached (symbolic-ref fails)
    assert git(recv, "symbolic-ref", "-q", "HEAD", check=False) == ""
    assert status(recv) == status(prod)


def test_first_time_apply_into_missing_repo(tmp_path):
    prod = tmp_path / "p"; make_base_repo(prod); make_mixed_state(prod)
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")

    recv = tmp_path / "brand_new"                        # does not exist yet
    res = grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk")
    assert res["created"] is True
    assert res["backup"] is None                         # nothing to back up
    assert status(recv) == status(prod)
    assert worktree_map(recv) == worktree_map(prod)


def test_mirror_branches_prunes_receiver_only(tmp_path):
    prod = tmp_path / "p"; make_base_repo(prod)
    git(prod, "branch", "shared")
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")

    recv = tmp_path / "r"; clone(prod, recv)
    git(recv, "branch", "receiver-only")                 # extra branch on receiver

    # default (additive): receiver-only is kept
    grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk1")
    assert "receiver-only" in git(recv, "for-each-ref",
                                  "--format=%(refname:short)", "refs/heads/").split()

    # mirror: receiver-only is pruned to match producer's branch set
    grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk2",
                   mirror_branches=True)
    names = git(recv, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split()
    assert "receiver-only" not in names
    assert set(names) == {"main", "shared"}


def test_capture_is_deterministic(tmp_path):
    """Same working state -> same snapshot object ids (stable bundle, no churn)."""
    prod = tmp_path / "p"; make_base_repo(prod); make_mixed_state(prod)
    s1 = grs.capture_state(prod)
    s2 = grs.capture_state(prod)
    assert (s1.cwork, s1.cindex, s1.head) == (s2.cwork, s2.cindex, s2.head)


def test_discover_repos(tmp_path):
    make_base_repo(tmp_path / "primary" / "app1")
    make_base_repo(tmp_path / "primary" / "nested" / "app2")
    (tmp_path / "primary" / "notes").mkdir(parents=True)       # not a repo
    (tmp_path / "primary" / ".venv").mkdir()
    make_base_repo(tmp_path / "primary" / ".venv" / "shouldskip")  # excluded

    repos = grs.discover_repos(tmp_path / "primary", exclude=[".venv*", "venv*"])
    names = {n for n, _ in repos}
    assert "app1" in names
    assert "nested__app2" in names
    assert not any("shouldskip" in n for n in names)


def test_preview_apply_no_mutation(tmp_path):
    prod = tmp_path / "p"; make_base_repo(prod); make_mixed_state(prod)
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")

    recv = tmp_path / "r"; clone(prod, recv)
    before = status(recv)
    prev = grs.preview_apply(recv, meta)
    assert prev["would_change"] is True
    assert prev["incoming_dirty"] is True
    assert status(recv) == before                              # untouched


# --------------------------------------------------------------------------- #
# CLI integration: `fsync git snapshot|status|apply` through main()           #
# --------------------------------------------------------------------------- #

def _write_git_config(home):
    cfg = home / ".config" / "fsync" / "sync-profiles.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "peer:\n  host: peer.invalid\n  user: dev\n  home: %s\n"
        "defaults:\n  conflict: newer\n"
        "profiles:\n"
        "  primary-repos:\n"
        "    kind: git\n"
        "    paths: [workspaces/primary]\n"
        "    exclude: ['.venv', 'node_modules']\n" % home
    )
    return cfg


def test_git_cli_snapshot_status_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from fsync.cli import main

    repo = tmp_path / "workspaces" / "primary" / "app"
    make_base_repo(repo)
    make_mixed_state(repo)
    target = status(repo)
    cfg = _write_git_config(tmp_path)

    assert main(["git", "snapshot", "--config", str(cfg)]) == 0
    assert (tmp_path / ".fsync" / "git-bundles" / "app.bundle").exists()
    assert (tmp_path / ".fsync" / "git-bundles" / "app.meta.json").exists()

    # status: no mutation
    assert main(["git", "status", "--config", str(cfg)]) == 0
    assert status(repo) == target

    # mutate the repo, then apply restores the exact snapshot state
    (repo / "a.txt").write_text("post-snapshot-divergence\n")
    (repo / "brand_new_stray.txt").write_text("stray\n")
    assert main(["git", "apply", "--config", str(cfg)]) == 0
    assert status(repo) == target
    assert not (repo / "brand_new_stray.txt").exists()          # stray removed

    # a recoverable pre-apply backup was written
    backups = list((tmp_path / ".fsync" / "backups").glob("*-git/app.pre-apply.bundle"))
    assert backups


def test_load_config_kind_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from fsync.homesync import HomesyncError, load_config

    good = tmp_path / "good.yaml"
    good.write_text("peer:\n  host: h\nprofiles:\n  x:\n    kind: git\n    paths: [a]\n")
    _peer, profiles, _d = load_config(str(good))
    assert profiles["x"].kind == "git"

    bad = tmp_path / "bad.yaml"
    bad.write_text("peer:\n  host: h\nprofiles:\n  x:\n    kind: bogus\n    paths: [a]\n")
    with pytest.raises(HomesyncError):
        load_config(str(bad))
