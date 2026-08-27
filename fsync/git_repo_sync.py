"""Full-fidelity git working-tree sync (P5 — docs/git-repo-sync.md).

fsync's file profiles are additive/newest-wins and cannot replicate a git
working tree (an exclude that matches a tracked path yields phantom deletions;
`.git` is a database, not a file tree). This module transports the *exact*
working state of a repo box-to-box with git's own tools:

- **Capture** snapshots the three trees that define a working state — HEAD, the
  index, and the full worktree (incl. untracked, non-ignored) — via
  ``write-tree`` + ``commit-tree``, then bundles everything into one
  internally-consistent file.
- **Apply** unconditionally backs up the receiver's own state first (R15), then
  reconstructs HEAD, staged, unstaged, and untracked exactly with two
  ``read-tree`` steps + a ``clean`` of strays.

Transport is a single ``git bundle`` per repo carried by an ordinary fsync
files-profile (stable filename, overwritten → newest-wins, no accumulation).
Nothing is ever pushed to an external remote (R14).
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

META_VERSION = 1
WIP_REF = "refs/fsync/wip"
INCOMING = "refs/fsync/incoming"

# Fixed identity/date for the synthetic snapshot commits: makes an unchanged
# working state hash to the SAME objects every run, so the bundle file is stable
# (newest-wins doesn't re-copy an identical snapshot) and never depends on the
# repo having a configured user.name/email.
_SNAP_ENV = {
    "GIT_AUTHOR_NAME": "fsync", "GIT_AUTHOR_EMAIL": "fsync@localhost",
    "GIT_COMMITTER_NAME": "fsync", "GIT_COMMITTER_EMAIL": "fsync@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
}


class GitSyncError(RuntimeError):
    """A git-repo-sync operation failed (git error, missing repo, etc.)."""


# --------------------------------------------------------------------------- #
# git plumbing                                                                #
# --------------------------------------------------------------------------- #

def _git(repo: str | Path, *args: str, index_file: str | None = None,
         input_text: str | None = None, check: bool = True,
         timeout: int = 300) -> subprocess.CompletedProcess:
    env = {**os.environ, **_SNAP_ENV,
           # Never let the user's global/system config change plumbing behaviour.
           "GIT_CONFIG_SYSTEM": os.environ.get("GIT_CONFIG_SYSTEM", "/dev/null")}
    if index_file is not None:
        env["GIT_INDEX_FILE"] = index_file
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env, timeout=timeout, input=input_text,
    )
    if check and proc.returncode != 0:
        raise GitSyncError(
            f"git {' '.join(args[:3])} failed in {repo} "
            f"(rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def _out(repo: str | Path, *args: str, **kw) -> str:
    return _git(repo, *args, **kw).stdout.strip()


def is_git_repo(path: str | Path) -> bool:
    """True if ``path`` is the TOP of a git working tree (not a subdir of one, not
    a bare repo). Uses ``--show-toplevel`` rather than a ``.git`` existence
    heuristic so linked worktrees (``.git`` file) work and ``.git`` symlinks to a
    deleted target don't misclassify."""
    p = Path(path)
    if not p.is_dir():
        return False
    top = _git(p, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0 or not top.stdout.strip():
        return False
    try:
        return Path(top.stdout.strip()).resolve() == p.resolve()
    except OSError:
        return False


def _has_head(repo: str | Path) -> bool:
    return _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0


def has_commits(repo: str | Path) -> bool:
    """True when the repo has a real HEAD to capture. False for an unborn HEAD
    (freshly ``git init``'d, or a HEAD pointing at a nonexistent branch): there
    is nothing to snapshot, so callers should skip rather than treat it as a
    failure."""
    return _has_head(repo)


def repo_busy(repo: str | Path) -> str | None:
    """A short reason string when the repo has an operation in flight — so we
    must neither snapshot nor mutate it — else None. Guards against racing a
    concurrent git process (index.lock) or clobbering an in-progress
    merge/rebase/cherry-pick/revert/bisect."""
    proc = _git(repo, "rev-parse", "--absolute-git-dir", check=False)
    if proc.returncode != 0:
        return None
    gd = Path(proc.stdout.strip())
    if (gd / "index.lock").exists():
        return "index.lock present (a git process is running)"
    for marker, label in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"),
                          ("rebase-apply", "rebase/am"), ("CHERRY_PICK_HEAD", "cherry-pick"),
                          ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect")):
        if (gd / marker).exists():
            return f"{label} in progress"
    return None


def special_warnings(repo: str | Path) -> list[str]:
    """Fidelity caveats worth surfacing: submodule working state and git-LFS
    object content don't travel in a working-tree bundle (v1 non-goals)."""
    repo = Path(repo)
    out: list[str] = []
    if (repo / ".gitmodules").exists():
        out.append("submodules present — nested submodule working state is NOT "
                   "captured (gitlink commit only)")
    ga = repo / ".gitattributes"
    try:
        if ga.exists() and "filter=lfs" in ga.read_text(errors="ignore"):
            out.append("git-LFS in use — LFS pointer files travel, not object content")
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- #
# discovery                                                                   #
# --------------------------------------------------------------------------- #

def _excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat.rstrip("/")) for pat in patterns)


def discover_repos(root: str | Path, exclude: list[str] | None = None) -> list[tuple[str, Path]]:
    """Find git repos under ``root``. Returns ``(name, path)`` pairs where name
    is the repo's path relative to ``root`` (``/`` → ``__``) so two repos with
    the same basename don't collide. Does not descend into a found repo, and
    skips directories whose basename matches an ``exclude`` pattern.
    """
    root = Path(root)
    exclude = exclude or []
    found: list[tuple[str, Path]] = []
    if not root.exists():
        return found
    if is_git_repo(root):
        return [(root.name, root)]
    for dirpath, dirnames, _files in os.walk(root):
        # prune excluded dirs in place so os.walk never descends into them
        dirnames[:] = [d for d in dirnames if not _excluded(d, exclude)]
        for d in list(dirnames):
            full = Path(dirpath) / d
            if is_git_repo(full):
                rel = full.relative_to(root).as_posix().replace("/", "__")
                found.append((rel, full))
                dirnames.remove(d)  # don't recurse into the repo
    found.sort()
    return found


# --------------------------------------------------------------------------- #
# capture (producer)                                                          #
# --------------------------------------------------------------------------- #

@dataclass
class RepoState:
    branch: str | None          # checked-out branch, or None if detached
    head: str                   # HEAD commit sha
    head_tree: str
    cindex: str                 # commit whose tree == the index (staged) state
    cwork: str                  # commit whose tree == full worktree (incl untracked)
    dirty: bool                 # worktree differs from HEAD in any way

    def to_meta(self) -> dict[str, Any]:
        return {
            "meta_version": META_VERSION,
            "captured_from": socket.gethostname(),
            "branch": self.branch,
            "head": self.head,
            "head_tree": self.head_tree,
            "cindex": self.cindex,
            "cwork": self.cwork,
            "dirty": self.dirty,
        }


def capture_state(repo: str | Path, *, ref: str = WIP_REF) -> RepoState:
    """Snapshot HEAD + index + full worktree into git objects, updating ``ref``
    to point at the worktree commit. Read-only w.r.t. the user's index, worktree,
    and branches (only new objects + ``ref`` are written)."""
    if not _has_head(repo):
        raise GitSyncError(f"{repo}: no commits yet (unborn HEAD) — nothing to capture")
    head = _out(repo, "rev-parse", "HEAD")
    head_tree = _out(repo, "rev-parse", "HEAD^{tree}")
    branch_proc = _git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    branch = branch_proc.stdout.strip() or None

    tindex = _out(repo, "write-tree")
    cindex = _out(repo, "commit-tree", tindex, "-p", head, "-m", "fsync:index")

    # Full worktree incl. untracked(non-ignored), built in a throwaway index so
    # the user's real index is never touched.
    tmp = tempfile.NamedTemporaryFile(prefix="fsync-idx-", delete=False)
    tmp.close()
    os.unlink(tmp.name)  # git creates it fresh
    try:
        _git(repo, "read-tree", head, index_file=tmp.name)
        _git(repo, "add", "-A", index_file=tmp.name)
        twork = _out(repo, "write-tree", index_file=tmp.name)
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
    cwork = _out(repo, "commit-tree", twork, "-p", head, "-p", cindex, "-m", "fsync:wip")
    _git(repo, "update-ref", ref, cwork)

    dirty = (tindex != head_tree) or (twork != head_tree)
    return RepoState(branch=branch, head=head, head_tree=head_tree,
                     cindex=cindex, cwork=cwork, dirty=dirty)


def _bundle(repo: str | Path, out_path: str | Path, *refs: str) -> int:
    """Write a bundle of the given refs; return its size in bytes."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    _git(repo, "bundle", "create", str(tmp), *refs)
    os.replace(tmp, out)  # atomic publish — a reader never sees a half-written bundle
    return out.stat().st_size


def snapshot_repo(repo: str | Path, bundle_dir: str | Path, *,
                  name: str | None = None, rel: str | None = None) -> dict[str, Any]:
    """Capture ``repo`` and write ``<name>.bundle`` + ``<name>.meta.json`` into
    ``bundle_dir`` (stable names, overwritten). ``rel`` is the repo's path
    relative to $HOME — recorded so the receiver can locate the same repo (both
    boxes share the home layout). Returns the meta dict augmented with bundle
    path + bytes."""
    repo = Path(repo)
    name = name or repo.name
    bundle_dir = Path(bundle_dir)
    busy = repo_busy(repo)
    if busy:
        raise GitSyncError(f"{repo}: {busy} — not snapshotting (repo not quiescent)")
    state = capture_state(repo)
    bundle_path = bundle_dir / f"{name}.bundle"
    size = _bundle(repo, bundle_path, "--branches", "--tags", WIP_REF, "HEAD")
    meta = state.to_meta()
    meta.update(name=name, rel=rel, bundle=str(bundle_path), bytes=size)
    (bundle_dir / f"{name}.meta.json").write_text(json.dumps(meta, indent=2))
    return meta


# --------------------------------------------------------------------------- #
# apply (receiver) — always-backup then reconstruct                          #
# --------------------------------------------------------------------------- #

def read_meta(bundle_dir: str | Path, name: str) -> dict[str, Any]:
    mp = Path(bundle_dir) / f"{name}.meta.json"
    if not mp.exists():
        raise GitSyncError(f"no meta for '{name}' at {mp}")
    return json.loads(mp.read_text())


def _backup_receiver(repo: str | Path, backup_dir: Path, name: str,
                     rel: str | None = None) -> str | None:
    """R15: bundle the receiver's own current state so an apply is reversible.
    Writes a sidecar meta (receiver branch/HEAD/dirty + rel + mode) so the bundle
    is interpretable by ``restore_backup``. Returns the backup bundle path, or
    None for an empty/unborn repo (nothing to lose)."""
    if not _has_head(repo):
        return None
    state = capture_state(repo, ref=WIP_REF)  # receiver's own 3-tree snapshot
    backup_dir.mkdir(parents=True, exist_ok=True)
    bpath = backup_dir / f"{name}.pre-apply.bundle"
    _bundle(repo, bpath, "--branches", "--tags", WIP_REF, "HEAD")
    (backup_dir / f"{name}.pre-apply.meta.json").write_text(
        json.dumps({**state.to_meta(), "name": name, "rel": rel, "mode": "bundle"}, indent=2))
    return str(bpath)


def _tar_dir(repo: Path, backup_dir: Path, name: str) -> str:
    """D1: preserve a populated NON-git directory verbatim before
    init-from-bundle + clean would wipe it. Returns the archive path."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    tar_path = backup_dir / f"{name}.pre-apply.dir.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(str(repo), arcname=".")
    return str(tar_path)


def _backup_ignored_collisions(repo: Path, cwork: str, backup_dir: Path, name: str) -> int:
    """D3: the pre-apply bundle (git add -A) skips git-ignored files, but
    reconstruct overwrites any receiver file at a path the producer tracks —
    ignored ones included. Copy those ignored collisions aside so nothing is lost.
    Returns the count saved. Requires the producer objects already fetched."""
    listing = _git(repo, "ls-tree", "-r", "-z", "--name-only", cwork).stdout
    producer_paths = [p for p in listing.split("\0") if p]
    existing = [p for p in producer_paths
                if (repo / p).is_symlink() or (repo / p).is_file()]
    if not existing:
        return 0
    # check-ignore reports only paths matching ignore rules AND not tracked —
    # exactly the files the normal bundle backup would miss.
    proc = _git(repo, "check-ignore", "-z", "--stdin",
                input_text="\0".join(existing) + "\0", check=False)
    ignored = [p for p in proc.stdout.split("\0") if p]
    if not ignored:
        return 0
    dest = backup_dir / f"{name}.ignored"
    for p in ignored:
        out = dest / p
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / p, out, follow_symlinks=False)
    return len(ignored)


def _reconstruct(repo: str | Path, meta: dict[str, Any], *, mirror_branches: bool) -> None:
    """Fetch producer refs from the (already-present) bundle objects and rebuild
    the exact working state described by ``meta``. Assumes objects were fetched
    into ``INCOMING``."""
    branch = meta["branch"]
    head = meta["head"]
    cwork = meta["cwork"]
    cindex = meta["cindex"]

    # Recreate every producer branch at its tip (fidelity of `git branch`).
    incoming_heads = _out(repo, "for-each-ref", "--format=%(refname)",
                          f"{INCOMING}/heads/").splitlines()
    producer_names = set()
    for ref in incoming_heads:
        bname = ref[len(f"{INCOMING}/heads/"):]
        producer_names.add(bname)
        tip = _out(repo, "rev-parse", ref)
        _git(repo, "update-ref", f"refs/heads/{bname}", tip)

    # Recreate producer tags too (fidelity of `git tag`); they were fetched.
    for ref in _out(repo, "for-each-ref", "--format=%(refname)",
                    f"{INCOMING}/tags/").splitlines():
        if ref:
            tname = ref[len(f"{INCOMING}/tags/"):]
            _git(repo, "update-ref", f"refs/tags/{tname}", _out(repo, "rev-parse", ref))

    # Point HEAD at the producer's checked-out branch (or detach at HEAD commit).
    if branch:
        _git(repo, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    else:
        _git(repo, "update-ref", "--no-deref", "HEAD", head)

    # Rebuild worktree + index exactly:
    #   worktree + index = full worktree (removes tracked strays)
    _git(repo, "read-tree", "--reset", "-u", f"{cwork}^{{tree}}")
    #   remove untracked strays not in the producer worktree. At this point the
    #   producer's own untracked files ARE in the index (from cwork), so clean
    #   keeps them and only deletes the receiver's leftovers.
    _git(repo, "clean", "-fdq")
    #   index = staged snapshot only (worktree untouched) -> restores the
    #   staged/unstaged/untracked split.
    _git(repo, "read-tree", f"{cindex}^{{tree}}")
    _git(repo, "update-index", "-q", "--refresh", check=False)

    # Optional mirror: prune local branches the producer no longer has. Safe
    # (R15 backed the receiver up) and only after HEAD moved onto a kept branch.
    if mirror_branches:
        local = _out(repo, "for-each-ref", "--format=%(refname:short)",
                     "refs/heads/").splitlines()
        for bname in local:
            if bname and bname not in producer_names:
                _git(repo, "branch", "-D", bname, check=False)

    # Tidy the incoming namespace (objects stay, reachable from refs/heads).
    for ref in _out(repo, "for-each-ref", "--format=%(refname)",
                    f"{INCOMING}/").splitlines():
        if ref:
            _git(repo, "update-ref", "-d", ref)


def apply_repo(repo: str | Path, bundle_path: str | Path, meta: dict[str, Any], *,
               backup_dir: str | Path, mirror_branches: bool = False,
               force: bool = False) -> dict[str, Any]:
    """Reconstruct ``meta``'s exact working state into ``repo`` from
    ``bundle_path``. Backs the receiver up first (R15). Creates the repo from the
    bundle when it does not exist yet (first sync). Refuses to move a receiver
    backwards — receiver HEAD not contained in the incoming head — unless
    ``force`` (ISSUE-006 sub-finding).
    """
    repo = Path(repo)
    bundle_path = Path(bundle_path)
    backup_dir = Path(backup_dir)
    name = meta.get("name", repo.name)
    if not bundle_path.exists():
        raise GitSyncError(f"bundle not found: {bundle_path}")

    created = not is_git_repo(repo)
    raw_backup = None
    if created:
        # D1: a populated NON-git directory holds real user files that
        # reconstruct's clean would wipe — preserve it verbatim first. An
        # empty/missing directory has nothing to lose.
        if repo.exists() and any(repo.iterdir()):
            raw_backup = _tar_dir(repo, backup_dir, name)
            (backup_dir / f"{name}.pre-apply.meta.json").write_text(
                json.dumps({"name": name, "rel": meta.get("rel"), "mode": "tar"}, indent=2))
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
    else:
        busy = repo_busy(repo)
        if busy:
            raise GitSyncError(f"{repo}: receiver {busy} — refusing to apply")

    # Fetch producer objects first (into INCOMING; never touches local refs) so
    # the backup can inspect the incoming tree for ignored-file collisions.
    _git(repo, "fetch", str(bundle_path),
         f"refs/heads/*:{INCOMING}/heads/*",
         f"refs/tags/*:{INCOMING}/tags/*",
         f"{WIP_REF}:{INCOMING}/wip")

    # ISSUE-006 sub-finding: never move a receiver BACKWARDS. Post-fetch the
    # incoming head object is present, so ancestry is decidable everywhere the
    # pre-fetch heuristic (check_receiver_ahead) is blind: a receiver HEAD not
    # reachable from the incoming head has commits the apply would drop off the
    # branch. The R15 backup would still hold them, but a silent rollback is
    # exactly the regression this guard exists to stop.
    if not created and not force and _has_head(repo):
        recv_head = _out(repo, "rev-parse", "HEAD")
        if recv_head != meta["head"]:
            anc = _git(repo, "merge-base", "--is-ancestor", recv_head, meta["head"],
                       check=False)
            if anc.returncode != 0:
                raise GitSyncError(
                    f"{name}: receiver has commits the incoming bundle lacks "
                    f"(receiver HEAD {recv_head[:9]} is not an ancestor of incoming "
                    f"{meta['head'][:9]}) — refusing to roll back; re-snapshot the "
                    f"producer, or re-run with --force"
                )

    backup = raw_backup
    saved_ignored = 0
    if not created:
        backup = _backup_receiver(repo, backup_dir, name, rel=meta.get("rel"))  # R15
        saved_ignored = _backup_ignored_collisions(repo, meta["cwork"],         # D3
                                                   backup_dir, name)

    try:
        _reconstruct(repo, meta, mirror_branches=mirror_branches)
    except GitSyncError as e:
        # R2: refs may have moved before the worktree finished — the repo can be
        # left partial. Point the operator at the recoverable backup.
        raise GitSyncError(
            f"{name}: apply failed mid-reconstruct ({e}); receiver may be partial "
            f"— restore from {backup or raw_backup or '(no backup: repo was empty)'}"
        ) from e

    return {
        "repo": str(repo),
        "name": name,
        "branch": meta["branch"],
        "head": meta["head"],
        "dirty": meta["dirty"],
        "created": created,
        "backup": backup,
        "saved_ignored": saved_ignored,
        "applied": True,
    }


def is_dirty(repo: str | Path) -> bool:
    """True if the working tree has any uncommitted change (tracked or untracked).
    Used by the guarded auto-apply to refuse clobbering a receiver the user may
    be editing (honors R17's intent)."""
    r = _git(repo, "status", "--porcelain", check=False)
    return bool((r.stdout or "").strip())


def check_branch_divergence(repo: str | Path, meta: dict[str, Any]) -> dict[str, Any] | None:
    """Return divergence info when the receiver is on a DIFFERENT branch than the
    producer, else None (ISSUE-001 guard, fix B).

    A producer-tracked file that the receiver's branch doesn't track lands
    untracked after apply — and, more surprisingly, apply silently moves the
    receiver onto the producer's branch (``_reconstruct`` sets HEAD to it). So a
    branch mismatch is exactly the condition worth stopping on. New/absent repos,
    or a receiver already on the producer's branch, are not divergence.
    """
    repo = Path(repo)
    if not is_git_repo(repo) or not _has_head(repo):
        return None  # first sync / unborn -> nothing to diverge from
    recv_branch = _git(repo, "symbolic-ref", "-q", "--short", "HEAD",
                       check=False).stdout.strip() or None
    prod_branch = meta.get("branch")
    if recv_branch == prod_branch:
        return None
    return {
        "receiver_branch": recv_branch,
        "producer_branch": prod_branch,
        "receiver_head": _out(repo, "rev-parse", "HEAD"),
        "producer_head": meta.get("head"),
    }


def check_receiver_ahead(repo: str | Path, meta: dict[str, Any]) -> dict[str, Any] | None:
    """Return rollback info when the incoming head is a strict ANCESTOR of the
    receiver's HEAD — the receiver holds commits the bundle predates, so applying
    would move the branch backwards (ISSUE-006 sub-finding).

    Pre-fetch heuristic: ancestry is only decidable when the receiver already has
    the incoming head object, which is exactly the stale-bundle case. An unknown
    incoming head (receiver behind, or true divergence) returns None here; the
    authoritative post-fetch check lives in ``apply_repo``.
    """
    repo = Path(repo)
    if not is_git_repo(repo) or not _has_head(repo):
        return None  # first sync / unborn -> nothing to roll back
    inc_head = meta.get("head")
    if not inc_head:
        return None
    recv_head = _out(repo, "rev-parse", "HEAD")
    if recv_head == inc_head:
        return None  # equal heads are the up-to-date/dirty-producer path
    r = _git(repo, "merge-base", "--is-ancestor", inc_head, recv_head, check=False)
    if r.returncode != 0:
        return None  # not an ancestor, or object unknown here
    return {"receiver_head": recv_head, "incoming_head": inc_head}


# Fix A (ISSUE-001): after a FILE profile transfers into a tree that contains git
# repos, files tracked on the producer's branch but not the receiver's arrive as
# untracked "ghosts". This probe reports exactly those — the transferred paths
# that git reports as untracked (``??``) on the receiving side. Runs on whichever
# box received the leg (local via subprocess, peer via ssh); args are the root and
# the transferred rel-paths, output is one ghost rel-path per line.
GHOST_PROBE = r'''
import os, subprocess, sys
from collections import defaultdict
root = sys.argv[1]
paths = sys.argv[2:]
groups, tl_cache = defaultdict(list), {}
def toplevel(d):
    if d not in tl_cache:
        r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True)
        tl_cache[d] = r.stdout.strip() if r.returncode == 0 else None
    return tl_cache[d]
for p in paths:
    ap = os.path.join(root, p)
    tl = toplevel(os.path.dirname(ap))
    if tl:
        groups[tl].append(ap)
out = set()
for tl, aps in groups.items():
    rels = [os.path.relpath(ap, tl) for ap in aps]
    r = subprocess.run(["git", "-C", tl, "status", "--porcelain",
                        "--untracked-files=all", "-z", "--"] + rels,
                       capture_output=True, text=True)
    for e in r.stdout.split("\x00"):
        if e.startswith("?? "):
            out.add(os.path.relpath(os.path.join(tl, e[3:]), root))
for g in sorted(out):
    print(g)
'''


def preview_apply(repo: str | Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Non-mutating: what an apply would change. Compares the receiver's current
    HEAD/dirty against the incoming bundle's."""
    repo = Path(repo)
    exists = is_git_repo(repo)
    local_head = _out(repo, "rev-parse", "HEAD", check=False) if exists and _has_head(repo) else None
    local_dirty = None
    if exists and _has_head(repo):
        local_dirty = bool(_out(repo, "status", "--porcelain"))
    return {
        "name": meta.get("name"),
        "repo": str(repo),
        "exists": exists,
        "local_head": local_head,
        "local_dirty": local_dirty,
        "incoming_head": meta["head"],
        "incoming_branch": meta["branch"],
        "incoming_dirty": meta["dirty"],
        "receiver_ahead": bool(check_receiver_ahead(repo, meta)) if exists else False,
        "would_change": (not exists) or local_head != meta["head"] or bool(local_dirty)
                        or meta["dirty"],
    }


# --------------------------------------------------------------------------- #
# restore + retention (P5.4): make R15 backups a one-command undo             #
# --------------------------------------------------------------------------- #

def restore_backup(repo: str | Path, backup_dir: str | Path, name: str) -> dict[str, Any]:
    """Inverse of ``apply_repo``: put the receiver's repo back to the exact state
    it was in before an apply, using the artifacts the apply wrote
    (``<name>.pre-apply.{bundle,meta.json,dir.tar.gz}`` + ``<name>.ignored/``)."""
    repo = Path(repo)
    backup_dir = Path(backup_dir)
    meta_p = backup_dir / f"{name}.pre-apply.meta.json"
    if not meta_p.exists():
        raise GitSyncError(f"no backup for '{name}' in {backup_dir}")
    meta = json.loads(meta_p.read_text())

    if meta.get("mode") == "tar":
        # D1 case: the receiver was a populated non-git dir. Wipe what apply
        # created and extract the original tree back.
        tar = backup_dir / f"{name}.pre-apply.dir.tar.gz"
        if not tar.exists():
            raise GitSyncError(f"tar backup missing: {tar}")
        if repo.exists():
            for child in repo.iterdir():
                shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        repo.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar) as tf:
            tf.extractall(repo)  # our own archive
        return {"repo": str(repo), "name": name, "mode": "tar"}

    # bundle case: reconstruct the receiver's own prior working state
    bundle = backup_dir / f"{name}.pre-apply.bundle"
    if not bundle.exists():
        raise GitSyncError(f"backup bundle missing: {bundle}")
    if not is_git_repo(repo):
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
    _git(repo, "fetch", str(bundle),
         f"refs/heads/*:{INCOMING}/heads/*",
         f"refs/tags/*:{INCOMING}/tags/*",
         f"{WIP_REF}:{INCOMING}/wip")
    _reconstruct(repo, meta, mirror_branches=False)

    # restore any ignored files apply had copied aside (producer overwrote them)
    restored_ignored = 0
    ign = backup_dir / f"{name}.ignored"
    if ign.is_dir():
        for src in ign.rglob("*"):
            if src.is_file() or src.is_symlink():
                out = repo / src.relative_to(ign)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, out, follow_symlinks=False)
                restored_ignored += 1
    return {"repo": str(repo), "name": name, "mode": "bundle",
            "branch": meta.get("branch"), "restored_ignored": restored_ignored}


def prune_backup_runs(backup_root: str | Path, keep: int = 10) -> int:
    """Keep only the newest ``keep`` per-run backup dirs (``*-git``); remove
    older ones. Timestamp-prefixed names sort chronologically. Returns the number
    removed."""
    backup_root = Path(backup_root)
    if keep <= 0 or not backup_root.exists():
        return 0
    runs = sorted(d for d in backup_root.iterdir() if d.is_dir() and d.name.endswith("-git"))
    victims = runs[:-keep] if len(runs) > keep else []
    for d in victims:
        shutil.rmtree(d, ignore_errors=True)
    return len(victims)
