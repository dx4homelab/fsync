"""SVN awareness for home-sync (ISSUE-004 / ISSUE-010).

Three layers, each usable without the next:

1. **Probe** — discover SVN working copies under a root and read their
   revision + URL, on this box or on the peer (one ssh round-trip, the probe is
   a stdlib-only script like ``git_repo_sync.GHOST_PROBE``).
2. **Stale-sender guard** (pure) — given both sides' probe results and the
   profile direction, name the checkouts a file profile must NOT carry this
   run: a sender whose working copy is behind the receiver's would re-create
   every file svn deleted in between (the 2026-11-06 incident), and a checkout
   whose URL differs between the boxes is a different branch at the same path.
3. **``kind: svn`` profiles** — mirror of ``kind: git``: the producer captures
   URL + revision + ``svn diff`` + the unversioned files; the receiver applies
   ``svn update -r REV`` + ``svn patch`` + the unversioned files, guarded so it
   can never lose work (clean, same URL, not ahead, not locked). Committed
   history never travels through fsync — the SVN server is the transport.

No module here imports ``homesync`` (it is imported BY it); peer access is a
``runner`` callable so the pure parts stay testable without ssh or svn.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable

META_VERSION = 1
PROBE_MAX_DEPTH = 6          # dirs below the profile root to look for .svn in
PROBE_TIMEOUT = 180
HEAD_TIMEOUT = 8             # per repository-root server round trip (preview only)
UNVERSIONED_FILE_CAP = 64 * 1024 * 1024   # a bigger unversioned file is listed, not carried
SVN_NET_ERRORS = ("E170013", "E175002", "E731001", "E670002", "Unable to connect",
                  "Could not resolve", "Connection refused", "Connection timed out")

Runner = Callable[..., subprocess.CompletedProcess]   # (command: str, *, timeout: int)


class SvnSyncError(RuntimeError):
    """An svn-sync operation failed (svn error, missing checkout, etc.)."""


class SvnServerUnreachable(SvnSyncError):
    """The SVN server did not answer — a standing, benign condition (VPN down),
    not a broken receiver."""


# --------------------------------------------------------------------------- #
# probe: working copies + revisions under a root (local or peer)              #
# --------------------------------------------------------------------------- #

# Runs with the box's own python3 + svn; stdlib only; one JSON object per
# checkout on stdout, plus a final {"heads": {...}} when --head is asked for
# (server HEAD per repository root, time-boxed — unreachable is a normal
# answer). Pruned dirs are VCS internals + toolchains that never hold a WC.
SVN_PROBE = r'''
import json, os, subprocess, sys
root = os.path.abspath(sys.argv[1]); max_depth = int(sys.argv[2])
want_head = "--head" in sys.argv[3:]
PRUNE = {".svn", ".git", "node_modules", "__pycache__", ".metadata", ".dart_tool",
         ".gradle", ".flutter-sdk", ".pub-cache", ".cache"}
def pruned(d):
    return d in PRUNE or d.startswith(".venv") or d.startswith("venv")
found = []
if os.path.isdir(root):
    for dirpath, dirnames, _ in os.walk(root):
        depth = 0 if dirpath == root else dirpath[len(root) + 1:].count(os.sep) + 1
        if ".svn" in dirnames and os.path.isdir(os.path.join(dirpath, ".svn")):
            found.append(dirpath)
        dirnames[:] = [d for d in dirnames if not pruned(d)] if depth < max_depth else []
roots = {}
for d in sorted(found):
    rel = os.path.relpath(d, root).replace(os.sep, "/")
    rec = {"rel": rel, "rev": None, "url": None, "root": None}
    try:
        r = subprocess.run(["svn", "info", "--non-interactive", d],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        rec["error"] = str(e)[:200]; print(json.dumps(rec)); continue
    if r.returncode != 0:
        rec["error"] = ((r.stderr or "").strip().splitlines() or ["svn info failed"])[-1][:200]
    else:
        for line in r.stdout.splitlines():
            if line.startswith("Revision: "):
                v = line[10:].strip(); rec["rev"] = int(v) if v.isdigit() else None
            elif line.startswith("URL: "):
                rec["url"] = line[5:].strip()
            elif line.startswith("Repository Root: "):
                rec["root"] = line[17:].strip()
    print(json.dumps(rec))
    if rec["root"]:
        roots[rec["root"]] = None
if want_head:
    for ru in roots:
        try:
            r = subprocess.run(["svn", "info", "--non-interactive", "--show-item", "revision", ru],
                               capture_output=True, text=True, timeout=%d)
            v = (r.stdout or "").strip()
            roots[ru] = int(v) if r.returncode == 0 and v.isdigit() else None
        except (OSError, subprocess.TimeoutExpired):
            roots[ru] = None
    print(json.dumps({"heads": roots}))
''' % HEAD_TIMEOUT


def parse_probe(stdout: str) -> tuple[dict[str, dict[str, Any]], dict[str, int | None]]:
    """``(checkouts_by_rel, heads_by_repo_root)`` from the probe's stdout."""
    found: dict[str, dict[str, Any]] = {}
    heads: dict[str, int | None] = {}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "heads" in rec and isinstance(rec["heads"], dict):
            heads.update(rec["heads"])
        elif rec.get("rel"):
            found[rec["rel"]] = rec
    return found, heads


def probe_local(root: str | Path, *, head: bool = False,
                max_depth: int = PROBE_MAX_DEPTH) -> tuple[dict[str, dict], dict[str, int | None]]:
    """Checkouts under ``root`` on this box. Best-effort: a failure is an empty
    answer (the caller decides whether that disables a guard)."""
    if not Path(root).is_dir():
        return {}, {}
    args = [sys.executable or "python3", "-c", SVN_PROBE, str(root), str(max_depth)]
    if head:
        args.append("--head")
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return {}, {}
    return parse_probe(proc.stdout) if proc.returncode == 0 else ({}, {})


def probe_remote(runner: Runner, root: str, *, head: bool = False,
                 max_depth: int = PROBE_MAX_DEPTH) -> tuple[dict[str, dict], dict[str, int | None]] | None:
    """Checkouts under ``root`` on the peer via ``runner`` (an ssh command
    runner). ``None`` when the probe itself could not run — distinct from
    "no checkouts there", because a guard must not silently stand down."""
    cmd = ("python3 -c " + shlex.quote(SVN_PROBE) + " " + shlex.quote(root) + f" {max_depth}"
           + (" --head" if head else ""))
    try:
        proc = runner(cmd, timeout=PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001 — the runner's own error type is the caller's
        return None
    if proc.returncode != 0:
        return None
    return parse_probe(proc.stdout)


# --------------------------------------------------------------------------- #
# stale-sender guard (pure)                                                   #
# --------------------------------------------------------------------------- #

def _short(host: str | None) -> str:
    return (host or "?").split(".")[0]


def stale_sender_guard(local: dict[str, dict], peer: dict[str, dict], *, direction: str,
                       local_host: str, peer_host: str,
                       heads: dict[str, int | None] | None = None) -> dict[str, Any]:
    """Decide, per checkout present on BOTH sides, whether a file profile may
    carry it this run.

    - ``push``: sender = local, receiver = peer; ``pull``: the reverse;
      ``both``: either leg may be the stale one, so any revision mismatch skips.
    - sender behind receiver  → **skipped** (resurrection hazard, ISSUE-010)
    - URL differs             → **skipped** (a different branch at the same path)
    - sender ahead of receiver → **advisory** only (receiver should ``svn update``;
      the leg cannot resurrect, it pre-delivers files the update would bring)
    - one side only / revision unknown → shown in ``parity``, no action

    Returns ``{"checked", "skipped": [...], "advisory": [...], "excludes": [...],
    "parity": [...]}``; ``excludes`` are ``<rel>/*`` patterns relative to the
    probed root, ready to append to the profile's exclude list. Pure.
    """
    lh, ph = _short(local_host), _short(peer_host)
    heads = heads or {}
    out: dict[str, Any] = {"checked": 0, "skipped": [], "advisory": [], "excludes": [],
                           "parity": []}
    for rel in sorted(set(local) | set(peer)):
        a, b = local.get(rel), peer.get(rel)
        head = None
        for rec in (a, b):
            if rec and rec.get("root") in heads and heads[rec["root"]] is not None:
                head = heads[rec["root"]]
        row = {"rel": rel, "local_rev": (a or {}).get("rev"), "peer_rev": (b or {}).get("rev"),
               "head_rev": head, "state": "ok", "reason": None}
        if a is None or b is None:
            row["state"] = "only-local" if b is None else "only-peer"
            out["parity"].append(row)
            continue
        out["checked"] += 1
        lr, pr = a.get("rev"), b.get("rev")
        if lr is None or pr is None:
            row["state"] = "unknown"
            row["reason"] = (a.get("error") or b.get("error") or "revision unknown")
            out["parity"].append(row)
            continue
        if a.get("url") and b.get("url") and a["url"] != b["url"]:
            row["state"] = "skip"
            row["reason"] = (f"{rel}: checkout URL differs ({lh} {a['url']}, {ph} {b['url']}) "
                             f"— a different branch at the same path, not file-synced")
        elif lr == pr:
            row["state"] = "ok"
        elif direction == "both":
            behind = lh if lr < pr else ph
            row["state"] = "skip"
            row["reason"] = (f"{rel}: {lh} r{lr} ≠ {ph} r{pr} — run svn update on {behind}, "
                             f"not file-synced")
        else:
            s_rev, r_rev = (lr, pr) if direction == "push" else (pr, lr)
            s_host, r_host = (lh, ph) if direction == "push" else (ph, lh)
            if s_rev < r_rev:
                row["state"] = "skip"
                row["reason"] = (f"{rel}: sender r{s_rev} < receiver r{r_rev} — run svn update "
                                 f"on {s_host}, not file-synced")
            else:
                row["state"] = "advisory"
                row["reason"] = (f"{rel}: sender r{s_rev} > receiver r{r_rev} — run svn update "
                                 f"on {r_host} (receiver is stale)")
        if row["state"] == "skip":
            out["skipped"].append({"rel": rel, "local_rev": lr, "peer_rev": pr,
                                   "reason": row["reason"]})
            out["excludes"].append(f"{rel}/*")
        elif row["state"] == "advisory":
            out["advisory"].append({"rel": rel, "local_rev": lr, "peer_rev": pr,
                                    "reason": row["reason"]})
        out["parity"].append(row)
    return out


# --------------------------------------------------------------------------- #
# kind: svn — working copy state                                              #
# --------------------------------------------------------------------------- #

def _svn(cwd: str | Path, *args: str, check: bool = True, timeout: int = 900,
         input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["svn", "--non-interactive", *args], cwd=str(cwd),
                              capture_output=True, text=True, timeout=timeout, input=input_text)
    except FileNotFoundError as e:
        raise SvnSyncError("svn is not installed on this box") from e
    except subprocess.TimeoutExpired as e:
        raise SvnSyncError(f"svn {' '.join(args[:2])} in {cwd}: no reply within {timeout}s") from e
    if check and proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-3:])
        if any(tok in tail for tok in SVN_NET_ERRORS):
            raise SvnServerUnreachable(f"svn {' '.join(args[:2])} in {cwd}: {tail}")
        raise SvnSyncError(f"svn {' '.join(args[:2])} in {cwd} failed (rc={proc.returncode}): {tail}")
    return proc


def is_svn_wc(path: str | Path) -> bool:
    return (Path(path) / ".svn").is_dir()


def _excluded(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat.rstrip("/")) for pat in patterns)


def discover_checkouts(root: str | Path, exclude: list[str] | None = None,
                       max_depth: int = PROBE_MAX_DEPTH) -> list[tuple[str, Path]]:
    """``(name, path)`` for every working copy under ``root`` (name = path
    relative to root with ``/`` → ``__``, like git repos). Nested checkouts are
    found too (``branches/`` may be a container WC with branch WCs inside);
    ``exclude`` prunes by directory basename."""
    root = Path(root)
    exclude = exclude or []
    found: list[tuple[str, Path]] = []
    if not root.is_dir():
        return found
    for dirpath, dirnames, _ in os.walk(root):
        d = Path(dirpath)
        depth = 0 if d == root else len(d.relative_to(root).parts)
        if (d / ".svn").is_dir():
            name = "." if d == root else d.relative_to(root).as_posix().replace("/", "__")
            found.append((root.name if name == "." else name, d))
        dirnames[:] = ([n for n in dirnames if n != ".svn" and not _excluded(n, exclude)]
                       if depth < max_depth else [])
    found.sort()
    return found


def wc_info(path: str | Path) -> dict[str, Any]:
    """``{"url", "root", "revision"}`` of a working copy."""
    out: dict[str, Any] = {"url": None, "root": None, "revision": None}
    proc = _svn(path, "info", ".")
    for line in proc.stdout.splitlines():
        if line.startswith("Revision: "):
            v = line[10:].strip()
            out["revision"] = int(v) if v.isdigit() else None
        elif line.startswith("URL: "):
            out["url"] = line[5:].strip()
        elif line.startswith("Repository Root: "):
            out["root"] = line[17:].strip()
    if not out["url"] or out["revision"] is None:
        raise SvnSyncError(f"{path}: svn info gave no URL/revision")
    return out


def parse_status(text: str) -> dict[str, Any]:
    """Classify ``svn status`` lines: ``modified`` (M/A/D/R/!/~ or property
    change), ``conflicted``, ``unversioned`` (?), ``locked`` (col 3 ``L``)."""
    st: dict[str, Any] = {"modified": [], "conflicted": [], "unversioned": [], "locked": False}
    for line in (text or "").splitlines():
        if len(line) < 9 or line.startswith(("Performing", "Status against", "---")):
            continue
        c1, c2, c3, c7, path = line[0], line[1], line[2], line[6], line[8:]
        if c3 == "L":
            st["locked"] = True
        if c1 == "C" or c2 == "C" or c7 == "C":
            st["conflicted"].append(path)
        elif c1 == "?":
            st["unversioned"].append(path)
        elif c1 in "ADMR!~" or c2 == "M":
            st["modified"].append(path)
    return st


def wc_status(path: str | Path) -> dict[str, Any]:
    return parse_status(_svn(path, "status", "--ignore-externals", ".").stdout)


def wc_busy(path: str | Path) -> str | None:
    """A locked working copy (an svn op in flight or one that died) must not be
    captured or applied to — the cure is ``svn cleanup``."""
    try:
        st = wc_status(path)
    except SvnSyncError as e:
        return f"status unavailable ({e})"
    if st["locked"]:
        return "working copy locked (svn cleanup needed, or an svn op in flight)"
    return None


# --------------------------------------------------------------------------- #
# snapshot (producer)                                                         #
# --------------------------------------------------------------------------- #

def _meta_path(snap_dir: Path, name: str) -> Path:
    return snap_dir / f"{name}.svn.meta.json"


def _walk_unversioned(wc: Path, entries: list[str], exclude: list[str]) -> tuple[list[str], list[str]]:
    """Expand ``?`` entries (files or dirs) into relative file paths, dropping
    excluded names and over-cap files. Returns (files, skipped_big)."""
    files: list[str] = []
    big: list[str] = []
    for rel in entries:
        p = wc / rel
        if _excluded(Path(rel).name, exclude):
            continue
        if p.is_symlink() or p.is_file():
            cands = [p]
        elif p.is_dir():
            cands = []
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if not _excluded(d, exclude)]
                cands.extend(Path(dirpath) / f for f in filenames if not _excluded(f, exclude))
        else:
            continue
        for f in cands:
            r = f.relative_to(wc).as_posix()
            try:
                if not f.is_symlink() and f.stat().st_size > UNVERSIONED_FILE_CAP:
                    big.append(r)
                    continue
            except OSError:
                continue
            files.append(r)
    return sorted(files), sorted(big)


def _sha(parts: Iterable[bytes]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.hexdigest()


def snapshot_wc(wc: str | Path, snap_dir: str | Path, *, name: str, rel: str | None,
                exclude: list[str] | None = None) -> dict[str, Any]:
    """Capture one working copy into ``snap_dir`` as ``<name>.svn.meta.json``
    + ``<name>.svn.diff`` (uncommitted versioned changes) + optionally
    ``<name>.svn.unversioned.tar.gz``. Stable names, overwritten each run.
    Read-only on the working copy."""
    wc = Path(wc)
    snap_dir = Path(snap_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    exclude = exclude or []
    busy = wc_busy(wc)
    if busy:
        raise SvnSyncError(f"{wc}: {busy} — not snapshotting")
    info = wc_info(wc)
    st = wc_status(wc)
    if st["conflicted"]:
        raise SvnSyncError(f"{wc}: {len(st['conflicted'])} conflicted path(s) — resolve before "
                           f"snapshotting: {', '.join(st['conflicted'][:3])}")
    diff_text = _svn(wc, "diff", "--internal-diff", ".").stdout if st["modified"] else ""
    diff_path = snap_dir / f"{name}.svn.diff"
    diff_path.write_text(diff_text)
    files, big = _walk_unversioned(wc, st["unversioned"], exclude)
    tar_path = snap_dir / f"{name}.svn.unversioned.tar.gz"
    tar_bytes = 0
    # fingerprint = names + sizes + mtimes: an unchanged set reuses last run's
    # tar instead of re-packing it every hour
    fp_parts = []
    for r in files:
        try:
            stt = (wc / r).lstat()
            fp_parts.append(f"{r}\0{stt.st_size}\0{stt.st_mtime_ns}".encode())
        except OSError:
            fp_parts.append(r.encode())
    unversioned_fp = _sha(fp_parts) if files else None
    prev_fp = None
    try:
        prev_fp = json.loads(_meta_path(snap_dir, name).read_text()).get("unversioned_fp")
    except (OSError, ValueError):
        pass
    if files and prev_fp == unversioned_fp and tar_path.exists():
        tar_bytes = tar_path.stat().st_size
    elif files:
        tmp = tar_path.with_suffix(".tmp")
        with tarfile.open(tmp, "w:gz") as tf:
            for r in files:
                tf.add(str(wc / r), arcname=r, recursive=False)
        os.replace(tmp, tar_path)
        tar_bytes = tar_path.stat().st_size
    elif tar_path.exists():
        tar_path.unlink()
    meta = {
        "version": META_VERSION, "name": name, "rel": rel,
        "url": info["url"], "root": info["root"], "revision": info["revision"],
        "dirty": bool(st["modified"]), "modified": st["modified"][:200],
        "unversioned": files[:500], "unversioned_count": len(files),
        "unversioned_skipped_big": big[:50],
        "diff": diff_path.name if diff_text else None,
        "unversioned_tar": tar_path.name if files else None,
        "unversioned_fp": unversioned_fp,
        "bytes": len(diff_text.encode()) + tar_bytes,
        "captured": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "producer": os.uname().nodename.split(".")[0],
    }
    # one id per distinct snapshot content: a receiver that already applied it
    # skips as "up to date" instead of re-patching every hour
    meta["snapshot_id"] = _sha([
        json.dumps([meta["url"], meta["revision"], meta["dirty"], meta["unversioned_count"]]).encode(),
        hashlib.sha256(diff_text.encode()).digest(),
        (unversioned_fp or "").encode(),
    ])
    _meta_path(snap_dir, name).write_text(json.dumps(meta, indent=2))
    return meta


def read_meta(snap_dir: str | Path, name: str) -> dict[str, Any]:
    mp = _meta_path(Path(snap_dir), name)
    if not mp.exists():
        raise SvnSyncError(f"no snapshot meta for '{name}' at {mp}")
    return json.loads(mp.read_text())


def load_metas(snap_dir: str | Path, only: str | None = None) -> list[dict[str, Any]]:
    out = []
    for mp in sorted(Path(snap_dir).expanduser().glob("*.svn.meta.json")):
        try:
            m = json.loads(mp.read_text())
        except (OSError, ValueError):
            continue
        if only and m.get("name") != only:
            continue
        out.append(m)
    return out


# --------------------------------------------------------------------------- #
# apply (receiver) — guarded, always-backup                                   #
# --------------------------------------------------------------------------- #

def _applied_store(state_dir: Path) -> Path:
    return state_dir / "svn-applied.json"


def _load_applied(state_dir: Path | None) -> dict[str, str]:
    if state_dir is None:
        return {}
    try:
        d = json.loads(_applied_store(state_dir).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_applied(state_dir: Path | None, applied: dict[str, str]) -> None:
    if state_dir is None:
        return
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = _applied_store(state_dir).with_suffix(".tmp")
        tmp.write_text(json.dumps(applied))
        os.replace(tmp, _applied_store(state_dir))
    except OSError:
        pass


def apply_decision(wc: str | Path, meta: dict[str, Any], *,
                   state_dir: Path | None = None) -> tuple[bool, str]:
    """Guard: apply only when it cannot lose work. ``(ok, reason)``; reasons
    are stable strings (sticky-streak keys on the caller's side)."""
    wc = Path(wc)
    if not wc.exists() or (wc.is_dir() and not any(wc.iterdir())):
        return True, "new"
    if not is_svn_wc(wc):
        return False, "receiver path exists but is not an svn working copy"
    busy = wc_busy(wc)
    if busy:
        return False, f"receiver {busy}"
    try:
        info = wc_info(wc)
        st = wc_status(wc)
    except SvnSyncError as e:
        return False, f"receiver unreadable ({e})"
    if info["url"] != meta.get("url"):
        return False, (f"checkout URL differs (receiver {info['url']}, producer {meta.get('url')}) "
                       f"— switch by hand, never auto-applied")
    if st["conflicted"] or st["modified"]:
        return False, "receiver has local changes"
    if info["revision"] is not None and meta.get("revision") is not None \
            and info["revision"] > meta["revision"]:
        return False, (f"receiver is ahead (r{info['revision']} > producer r{meta['revision']}) "
                       f"— stale snapshot; svn update the producer")
    if _load_applied(state_dir).get(meta.get("name", "")) == meta.get("snapshot_id") \
            and info["revision"] == meta.get("revision"):
        return False, "up to date"
    if info["revision"] == meta.get("revision") and not meta.get("dirty") \
            and not meta.get("unversioned_tar"):
        return False, "up to date"
    return True, "clean"


def _backup_receiver(wc: Path, backup_dir: Path, name: str, meta: dict[str, Any],
                     incoming_files: list[str]) -> dict[str, Any]:
    """R15 for svn: the receiver is clean (guard), so what an apply can clobber
    is (a) its revision, restorable from the server, and (b) its own unversioned
    files at paths the incoming tar carries. Record (a), tar (b)."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    info = wc_info(wc)
    collide = [r for r in incoming_files if (wc / r).is_file() or (wc / r).is_symlink()]
    tar = None
    if collide:
        tar = backup_dir / f"{name}.pre-apply.unversioned.tar.gz"
        with tarfile.open(tar, "w:gz") as tf:
            for r in collide:
                tf.add(str(wc / r), arcname=r, recursive=False)
    bmeta = {"name": name, "rel": meta.get("rel"), "url": info["url"],
             "revision": info["revision"], "unversioned_tar": tar.name if tar else None,
             "collisions": len(collide), "mode": "svn"}
    (backup_dir / f"{name}.pre-apply.meta.json").write_text(json.dumps(bmeta, indent=2))
    return bmeta


def force_clean(wc: str | Path, backup_dir: str | Path, name: str) -> str:
    """Save the receiver's uncommitted versioned changes as
    ``<name>.pre-apply.diff`` and revert them, so a forced apply loses nothing
    irretrievably. Returns the diff path."""
    wc = Path(wc)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    diff = backup_dir / f"{name}.pre-apply.diff"
    diff.write_text(_svn(wc, "diff", "--internal-diff", ".").stdout)
    _svn(wc, "revert", "-R", ".")
    return str(diff)


def _tar_members(tar_path: Path) -> list[str]:
    with tarfile.open(tar_path, "r:gz") as tf:
        return [m.name for m in tf.getmembers() if m.isfile() or m.issym()]


def _safe_extract(tar_path: Path, dest: Path) -> int:
    n = 0
    with tarfile.open(tar_path, "r:gz") as tf:
        for m in tf.getmembers():
            p = Path(m.name)
            if p.is_absolute() or ".." in p.parts:
                continue
            if m.isfile() or m.issym():
                tf.extract(m, path=str(dest), filter="data") if hasattr(tarfile, "data_filter") \
                    else tf.extract(m, path=str(dest))
                n += 1
    return n


def apply_wc(wc: str | Path, snap_dir: str | Path, meta: dict[str, Any], *,
             backup_dir: str | Path, state_dir: Path | None = None) -> dict[str, Any]:
    """Bring ``wc`` to the producer's exact state: ``svn update -r REV`` (or a
    fresh checkout), ``svn patch`` the diff, unpack the unversioned files. The
    caller runs ``apply_decision`` first; this only re-checks the cheap
    invariants. Backs the receiver up first."""
    wc = Path(wc)
    snap_dir = Path(snap_dir)
    backup_dir = Path(backup_dir)
    name = meta.get("name", wc.name)
    rev = meta.get("revision")
    url = meta.get("url")
    if not url or rev is None:
        raise SvnSyncError(f"{name}: snapshot meta lacks url/revision")
    tar_path = snap_dir / meta["unversioned_tar"] if meta.get("unversioned_tar") else None
    diff_path = snap_dir / meta["diff"] if meta.get("diff") else None
    incoming = _tar_members(tar_path) if tar_path and tar_path.exists() else []

    created = not is_svn_wc(wc)
    backup = None
    if created:
        if wc.exists() and any(wc.iterdir()):
            raise SvnSyncError(f"{name}: {wc} exists and is not empty — refusing to check out over it")
        wc.parent.mkdir(parents=True, exist_ok=True)
        _svn(wc.parent, "checkout", "-r", str(rev), url, str(wc))
    else:
        busy = wc_busy(wc)
        if busy:
            raise SvnSyncError(f"{name}: receiver {busy} — refusing to apply")
        backup = _backup_receiver(wc, backup_dir, name, meta, incoming)
        if wc_info(wc)["revision"] != rev:
            _svn(wc, "update", "-r", str(rev), "--accept", "postpone", ".")
    patched = False
    if diff_path and diff_path.exists() and diff_path.stat().st_size:
        try:
            _svn(wc, "patch", str(diff_path), ".")
        except SvnSyncError as e:
            raise SvnSyncError(f"{name}: svn patch failed mid-apply ({e}); receiver may be "
                               f"partial — `svn revert -R .` in {wc} restores r{rev}") from e
        patched = True
    unpacked = 0
    if tar_path and tar_path.exists():
        unpacked = _safe_extract(tar_path, wc)
    applied = _load_applied(state_dir)
    applied[name] = meta.get("snapshot_id", "")
    _save_applied(state_dir, applied)
    return {"wc": str(wc), "name": name, "revision": rev, "created": created,
            "patched": patched, "unpacked": unpacked,
            "backup": str(backup_dir) if backup else None, "applied": True}


def preview_apply(wc: str | Path, meta: dict[str, Any], *, state_dir: Path | None = None) -> dict[str, Any]:
    wc = Path(wc)
    exists = is_svn_wc(wc)
    out: dict[str, Any] = {"exists": exists, "would_change": True, "receiver_revision": None,
                           "receiver_ahead": False, "reason": None}
    if exists:
        try:
            out["receiver_revision"] = wc_info(wc)["revision"]
        except SvnSyncError:
            pass
    ok, reason = apply_decision(wc, meta, state_dir=state_dir)
    out["would_change"] = ok
    out["reason"] = reason
    out["receiver_ahead"] = reason.startswith("receiver is ahead")
    return out


def restore_backup(wc: str | Path, backup_dir: str | Path, name: str) -> dict[str, Any]:
    """Undo an apply: revert the patch, go back to the pre-apply revision, put
    the receiver's own clobbered unversioned files back. Files the patch ADDED
    stay on disk unversioned (svn revert leaves them) — listed in the result."""
    wc = Path(wc)
    backup_dir = Path(backup_dir)
    mp = backup_dir / f"{name}.pre-apply.meta.json"
    if not mp.exists():
        raise SvnSyncError(f"no pre-apply backup for '{name}' in {backup_dir}")
    bmeta = json.loads(mp.read_text())
    if not is_svn_wc(wc):
        raise SvnSyncError(f"{wc}: not a working copy — nothing to restore into")
    before = wc_status(wc)
    _svn(wc, "revert", "-R", ".")
    if bmeta.get("revision") is not None:
        _svn(wc, "update", "-r", str(bmeta["revision"]), "--accept", "postpone", ".")
    restored = 0
    if bmeta.get("unversioned_tar"):
        restored = _safe_extract(backup_dir / bmeta["unversioned_tar"], wc)
    return {"wc": str(wc), "revision": bmeta.get("revision"), "restored_unversioned": restored,
            "left_unversioned": before["modified"][:50], "mode": "svn"}


def prune_backup_runs(backup_root: str | Path, keep: int = 10) -> int:
    root = Path(backup_root).expanduser()
    if keep <= 0 or not root.is_dir():
        return 0
    runs = sorted((d for d in root.iterdir() if d.is_dir() and d.name.endswith("-svnapply")),
                  key=lambda d: d.name)
    removed = 0
    for d in runs[:-keep] if len(runs) > keep else []:
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed
