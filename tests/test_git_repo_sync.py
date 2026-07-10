"""P5 git-repo-sync: full-fidelity round-trip + safety tests.

The core guarantee (R13): snapshot -> bundle -> apply reproduces the producer's
EXACT `git status` (staged / unstaged / untracked / deletions), branch set,
HEAD, and worktree content on the receiver. Plus R15: every apply leaves a
recoverable pre-apply backup of the receiver's own work.
"""

from __future__ import annotations

import hashlib
import os
import shutil
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


# --------------------------------------------------------------------------- #
# P5.3 hardening: adversarial edge cases + safety guards                       #
# --------------------------------------------------------------------------- #

def test_untracked_collision_and_dir_file_flip(tmp_path):
    """A receiver untracked file colliding with an incoming tracked path, and a
    dir<->file type flip, must both resolve to the producer's state (R15 makes
    the clobber safe; `read-tree --reset -u` forces it)."""
    prod = tmp_path / "p"; make_base_repo(prod)
    (prod / "collide.txt").write_text("PRODUCER\n"); git(prod, "add", "collide.txt")
    target = status(prod)
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")

    recv = tmp_path / "r"; clone(prod, recv)
    (recv / "collide.txt").write_text("RECEIVER-UNTRACKED\n")   # collides w/ producer tracked
    shutil.rmtree(recv / "sub"); (recv / "sub").write_text("was-a-dir\n")   # dir -> file

    grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk")
    assert status(recv) == target
    assert (recv / "collide.txt").read_text() == "PRODUCER\n"
    assert (recv / "sub" / "c.txt").read_text() == "nested\n"   # dir restored


def test_symlink_and_exec_bit_fidelity(tmp_path):
    prod = tmp_path / "p"; make_base_repo(prod)
    (prod / "run.sh").write_text("#!/bin/sh\necho hi\n"); os.chmod(prod / "run.sh", 0o755)
    (prod / "link").symlink_to("a.txt")
    git(prod, "add", "-A"); git(prod, "commit", "-qm", "c2")
    (prod / "u.sh").write_text("#!/bin/sh\n"); os.chmod(prod / "u.sh", 0o755)   # untracked exec
    (prod / "ulink").symlink_to("b.txt")                                        # untracked symlink
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")

    recv = tmp_path / "r"; clone(prod, recv)
    grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk")
    assert status(recv) == status(prod)
    assert os.access(recv / "run.sh", os.X_OK)
    assert os.access(recv / "u.sh", os.X_OK)
    assert os.path.islink(recv / "link") and os.readlink(recv / "link") == "a.txt"
    assert os.path.islink(recv / "ulink") and os.readlink(recv / "ulink") == "b.txt"


def test_slash_branch_and_mirror_prune(tmp_path):
    prod = tmp_path / "p"; make_base_repo(prod)
    git(prod, "checkout", "-q", "-b", "feature/x")
    (prod / "b.txt").write_text("on-feature\n")               # dirty on a slashed branch
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")
    assert meta["branch"] == "feature/x"

    recv = tmp_path / "r"; clone(prod, recv)
    git(recv, "branch", "local/only")
    grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk",
                   mirror_branches=True)
    assert git(recv, "symbolic-ref", "--short", "HEAD") == "feature/x"
    names = set(git(recv, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())
    assert "feature/x" in names and "local/only" not in names
    assert status(recv) == status(prod)


def test_repo_busy_detection(tmp_path):
    repo = tmp_path / "r"; make_base_repo(repo)
    assert grs.repo_busy(repo) is None
    lock = repo / ".git" / "index.lock"; lock.write_text("")
    assert grs.repo_busy(repo) and "index.lock" in grs.repo_busy(repo)
    lock.unlink()
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    assert "merge" in grs.repo_busy(repo)


def test_apply_refuses_busy_receiver(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from fsync.cli import main

    repo = tmp_path / "workspaces" / "primary" / "app"
    make_base_repo(repo); make_mixed_state(repo)
    cfg = _write_git_config(tmp_path)
    assert main(["git", "snapshot", "--config", str(cfg)]) == 0

    before = status(repo)
    (repo / ".git" / "index.lock").write_text("")               # receiver "busy"
    rc = main(["git", "apply", "--config", str(cfg)])
    (repo / ".git" / "index.lock").unlink()
    assert rc == 1
    assert status(repo) == before                               # untouched


def test_special_warnings(tmp_path):
    repo = tmp_path / "r"; make_base_repo(repo)
    assert grs.special_warnings(repo) == []
    (repo / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n')
    (repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs\n")
    w = grs.special_warnings(repo)
    assert any("submodule" in x for x in w)
    assert any("LFS" in x for x in w)


def test_apply_into_populated_non_git_dir_is_backed_up(tmp_path):
    """D1 (data-loss): a populated NON-git dir must be preserved before the
    init-from-bundle path's clean would wipe it."""
    prod = tmp_path / "p"; make_base_repo(prod); make_mixed_state(prod)
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")

    recv = tmp_path / "recv"; recv.mkdir()
    (recv / "precious.txt").write_text("DO NOT LOSE\n")
    (recv / "nested").mkdir(); (recv / "nested" / "k.txt").write_text("keep\n")

    res = grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk")
    assert res["created"] is True
    assert res["backup"] and res["backup"].endswith(".tar.gz")
    assert status(recv) == status(prod)             # producer state applied
    assert not (recv / "precious.txt").exists()     # overwritten...
    import tarfile                                   # ...but recoverable from the tar
    with tarfile.open(res["backup"]) as tf:
        names = tf.getnames()
    assert any(n.endswith("precious.txt") for n in names)
    assert any(n.endswith("nested/k.txt") for n in names)


def test_apply_ignored_collision_is_recoverable(tmp_path):
    """D3 (data-loss): a receiver git-ignored file at a path the producer now
    TRACKS is overwritten, but must be saved to the backup first."""
    prod = tmp_path / "p"; make_base_repo(prod)
    recv = tmp_path / "r"; clone(prod, recv)                 # receiver at c1
    (recv / ".gitignore").write_text("config.local\n")
    (recv / "config.local").write_text("RECEIVER-SECRET\n")  # ignored, untracked

    # producer now tracks config.local
    (prod / "config.local").write_text("PRODUCER-CONFIG\n")
    git(prod, "add", "config.local"); git(prod, "commit", "-qm", "c2")
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")

    res = grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk")
    assert res["saved_ignored"] == 1
    assert (recv / "config.local").read_text() == "PRODUCER-CONFIG\n"   # producer wins (R13)
    saved = tmp_path / "bk" / "repo.ignored" / "config.local"
    assert saved.read_text() == "RECEIVER-SECRET\n"                     # recoverable (R15)


def test_tags_are_reconstructed(tmp_path):
    """F3: tags travel and are recreated on the receiver."""
    prod = tmp_path / "p"; make_base_repo(prod)
    git(prod, "tag", "v1.0")
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="repo")

    recv = tmp_path / "fresh"                                # init-from-bundle
    grs.apply_repo(recv, meta["bundle"], meta, backup_dir=tmp_path / "bk")
    assert "v1.0" in git(recv, "tag").split()


# --------------------------------------------------------------------------- #
# P5.4 polish: restore (undo an apply) + backup retention                      #
# --------------------------------------------------------------------------- #

def test_restore_backup_reverts_apply(tmp_path):
    """restore_backup returns the receiver to its exact pre-apply state,
    including a git-ignored file the apply had overwritten (D3 + restore)."""
    prod = tmp_path / "p"; make_base_repo(prod)
    recv = tmp_path / "r"; clone(prod, recv)
    (recv / ".gitignore").write_text("secret\n")
    (recv / "secret").write_text("RECEIVER-SECRET\n")       # ignored
    (recv / "b.txt").write_text("receiver-edit\n")          # tracked unstaged edit
    pre_status = status(recv)
    pre_b = (recv / "b.txt").read_text()

    (prod / "secret").write_text("PRODUCER\n")              # producer now tracks it
    git(prod, "add", "secret"); git(prod, "commit", "-qm", "c2")
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="app")
    bk = tmp_path / "bk"
    grs.apply_repo(recv, meta["bundle"], meta, backup_dir=bk)
    assert (recv / "secret").read_text() == "PRODUCER\n"    # apply won

    res = grs.restore_backup(recv, bk, "app")
    assert res["mode"] == "bundle"
    assert res["restored_ignored"] == 1
    assert status(recv) == pre_status                       # exact prior state
    assert (recv / "b.txt").read_text() == pre_b
    assert (recv / "secret").read_text() == "RECEIVER-SECRET\n"   # ignored restored


def test_restore_backup_tar_mode(tmp_path):
    """D1 restore: a populated non-git dir comes back verbatim, no .git left."""
    prod = tmp_path / "p"; make_base_repo(prod)
    bundles = tmp_path / "bundles"
    meta = grs.snapshot_repo(prod, bundles, name="app")
    recv = tmp_path / "recv"; recv.mkdir()
    (recv / "precious.txt").write_text("KEEP\n")
    bk = tmp_path / "bk"
    grs.apply_repo(recv, meta["bundle"], meta, backup_dir=bk)   # created -> tar backup
    assert not (recv / "precious.txt").exists()

    res = grs.restore_backup(recv, bk, "app")
    assert res["mode"] == "tar"
    assert (recv / "precious.txt").read_text() == "KEEP\n"
    assert not (recv / ".git").exists()                         # back to a plain dir


def test_git_profile_snapshots_only_on_producer():
    """A pull-only (receiver) git profile must NOT snapshot in `sync run` — else
    it races the producer's bundles under newest-wins."""
    from fsync.homesync import Profile, git_profile_snapshots
    mk = lambda kind, direction: Profile(name="r", paths=["p"], kind=kind, direction=direction)
    assert git_profile_snapshots(mk("git", "push")) is True
    assert git_profile_snapshots(mk("git", "both")) is True
    assert git_profile_snapshots(mk("git", "pull")) is False    # receiver
    assert git_profile_snapshots(mk("files", "push")) is False  # not a git profile


def test_prune_backup_runs(tmp_path):
    root = tmp_path / "backups"; root.mkdir()
    for ts in ("20260101-000001-git", "20260101-000002-git", "20260101-000003-git"):
        (root / ts).mkdir()
    (root / "unrelated").mkdir()                                 # not a -git run
    removed = grs.prune_backup_runs(root, keep=2)
    assert removed == 1
    assert not (root / "20260101-000001-git").exists()
    assert (root / "20260101-000002-git").exists()
    assert (root / "20260101-000003-git").exists()
    assert (root / "unrelated").exists()


def test_git_cli_restore_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from fsync.cli import main

    repo = tmp_path / "workspaces" / "primary" / "app"
    make_base_repo(repo); make_mixed_state(repo)
    cfg = _write_git_config(tmp_path)
    assert main(["git", "snapshot", "--config", str(cfg)]) == 0
    snap_status = status(repo)

    (repo / "a.txt").write_text("diverged\n")               # move away from snapshot
    assert main(["git", "apply", "--config", str(cfg)]) == 0
    assert status(repo) == snap_status                      # applied
    # now restore undoes the apply back to the receiver's pre-apply state
    assert main(["git", "restore", "--config", str(cfg)]) == 0
    assert (repo / "a.txt").read_text() == "diverged\n"     # receiver's prior edit is back
