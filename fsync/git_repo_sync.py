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
import socket
import subprocess
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
         check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    env = {**os.environ, **_SNAP_ENV,
           # Never let the user's global/system config change plumbing behaviour.
           "GIT_CONFIG_SYSTEM": os.environ.get("GIT_CONFIG_SYSTEM", "/dev/null")}
    if index_file is not None:
        env["GIT_INDEX_FILE"] = index_file
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env, timeout=timeout,
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
    """True if ``path`` is the top of a git working tree (has a .git)."""
    p = Path(path)
    if not p.is_dir():
        return False
    return _git(p, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0 \
        and (p / ".git").exists()


def _has_head(repo: str | Path) -> bool:
    return _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0


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


def _backup_receiver(repo: str | Path, backup_dir: Path, name: str) -> str | None:
    """R15: bundle the receiver's own current state so an apply is reversible.
    Returns the backup bundle path, or None for an empty/unborn repo (nothing to
    lose)."""
    if not _has_head(repo):
        return None
    capture_state(repo, ref=WIP_REF)  # receiver's own 3-tree snapshot
    backup_dir.mkdir(parents=True, exist_ok=True)
    bpath = backup_dir / f"{name}.pre-apply.bundle"
    _bundle(repo, bpath, "--branches", "--tags", WIP_REF, "HEAD")
    return str(bpath)


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
               backup_dir: str | Path, mirror_branches: bool = False) -> dict[str, Any]:
    """Reconstruct ``meta``'s exact working state into ``repo`` from
    ``bundle_path``. Backs the receiver up first (R15). Creates the repo from the
    bundle when it does not exist yet (first sync).
    """
    repo = Path(repo)
    bundle_path = Path(bundle_path)
    backup_dir = Path(backup_dir)
    name = meta.get("name", repo.name)
    if not bundle_path.exists():
        raise GitSyncError(f"bundle not found: {bundle_path}")

    created = False
    if not is_git_repo(repo):
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
        created = True

    backup = None if created else _backup_receiver(repo, backup_dir, name)

    # Bring producer objects/refs in from the bundle (never touches local refs).
    _git(repo, "fetch", str(bundle_path),
         f"refs/heads/*:{INCOMING}/heads/*",
         f"refs/tags/*:{INCOMING}/tags/*",
         f"{WIP_REF}:{INCOMING}/wip")
    _reconstruct(repo, meta, mirror_branches=mirror_branches)

    return {
        "repo": str(repo),
        "name": name,
        "branch": meta["branch"],
        "head": meta["head"],
        "dirty": meta["dirty"],
        "created": created,
        "backup": backup,
        "applied": True,
    }


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
        "would_change": (not exists) or local_head != meta["head"] or bool(local_dirty)
                        or meta["dirty"],
    }
