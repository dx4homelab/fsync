"""Profile-driven home synchronisation between two boxes (P1: headless engine).

Design doc: ``docs/home-sync-automation.md``. fsync plans and verifies; rsync
moves the bytes. Streamlined profiles run end-to-end with no prompts: conflicts
auto-resolve (default newest-wins) and every overwritten file is preserved on
the receiving box under a per-run backup dir (rsync ``--backup-dir``), so runs
are hands-off yet reversible. The ``review`` policy (the .claude profile) holds
conflicts in the run report instead.

Both sides hash locally: the peer runs ``fsync index`` over SSH from an engine
copy pushed to ``~/.local/lib/fsync-engine`` and only index JSON crosses the
LAN. Hashing is incremental via the per-root cache (unchanged size+mtime skips
the read), so a no-change run costs seconds.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fileindex import (
    DEFAULT_RSYNC_SSH,
    build_sync_plan,
    compare_file_lists,
    default_cache_path,
    list_files_with_metadata,
)

DEFAULT_CONFIG = "~/.config/fsync/sync-profiles.yaml"
STATE_ROOT = "~/.local/state/fsync"
ENGINE_DIR = ".local/lib/fsync-engine"  # relative to the peer's $HOME

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=yes"]
# rsync exits that mean "the sync itself is fine": 24 = source files vanished
# mid-transfer (live trees do that), 23 = partial transfer (typically the same
# vanish reported by the sender, or an unreadable file — logged, never fatal).
TOLERATED_RSYNC_EXITS = (0, 23, 24)

DEFAULT_PROFILES_YAML = """\
# fsync home-sync profiles. Design: docs/home-sync-automation.md (fsync repo).
#
# conflict: newer  = same-path/different-content auto-resolves by mtime; the
#                    LOSING version is preserved on the receiver under
#                    <backup_root>/<run-id>/<profile>/ — nothing is destroyed.
# conflict: review = conflicts are held in the run report, only additive
#                    copies happen (the .claude pipeline).
peer:
  host: minis4dx.lan   # router DNS name; bare hostname resolution is not reliable here
  user: developer
  home: /var/home/developer
defaults:
  conflict: newer
  rename_min_size: 64
  workers: 8
  backup_root: ~/.fsync/backups
profiles:
  documents:
    paths: [Documents, Pictures, Desktop, eclipse-workspace]
  tools:
    paths: [tools]
  secrets:
    paths: [secrets]
  dotfiles:
    paths: ["."]
    recursive: false
    exclude: ["*.tmp", ".bash_history*", "*.log", ".claude.json*"]
  claude:
    # Durable artifacts only (memories, agents, skills, plugins). Session
    # ephemera (transcripts, file-history, todos, plans, caches) are managed
    # by Claude Code's own ~30-day GC: syncing them just ping-pongs — the
    # receiver's next cleanup deletes them and the next sync re-copies them.
    paths: [.claude]
    conflict: review
    exclude: [
      ".credentials.json", "backups/*", "statsig/*", "shell-snapshots/*",
      "file-history/*", "projects/*.jsonl", "todos/*", "tasks/*", "plans/*",
      "cache/*", "debug/*", "session-env/*",
    ]
"""


class HomesyncError(RuntimeError):
    """A hard error that should fail the run (unlike vanished-file noise)."""


# --------------------------------------------------------------------------- #
# run state: progress snapshots + current-run pointer (what the TUI attaches  #
# to; the runner owns the files, any number of UIs may read them)             #
# --------------------------------------------------------------------------- #

def state_root() -> Path:
    return Path(STATE_ROOT).expanduser()


def current_run_pointer() -> Path:
    return state_root() / "current-run.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def discover_run() -> dict[str, Any] | None:
    """Locate the most recent run for a UI to attach to.

    Returns ``{"pointer": ..., "progress": ..., "running": bool}`` or None.
    ``running`` is true only when the runner process is still alive AND its
    progress says so — a crashed runner shows up as not-running with a stale
    "running" status, which the UI should render as aborted.
    """
    ptr_path = current_run_pointer()
    try:
        pointer = json.loads(ptr_path.read_text())
        progress = json.loads((Path(pointer["run_dir"]) / "progress.json").read_text())
    except (OSError, ValueError, KeyError):
        return None
    try:
        os.kill(int(pointer["pid"]), 0)
        alive = True
    except (OSError, ValueError):
        alive = False
    return {"pointer": pointer, "progress": progress,
            "running": alive and progress.get("status") == "running"}


class ProgressWriter:
    """Owns ``<run_dir>/progress.json``: one atomically-replaced snapshot of
    the whole run state, throttled so per-chunk rsync updates stay cheap.
    Readers (the TUI) just poll and render the latest snapshot."""

    def __init__(self, run_dir: Path, run_id: str, dry_run: bool, profile_names: list[str]):
        self.path = run_dir / "progress.json"
        self.state: dict[str, Any] = {
            "run_id": run_id,
            "pid": os.getpid(),
            "run_dir": str(run_dir),
            "started_ts": time.time(),
            "dry_run": dry_run,
            "status": "running",
            "profiles": {n: {"status": "pending", "paths": {}} for n in profile_names},
            "errors": [],
            "finished_ts": None,
        }
        self._last_write = 0.0
        self.write(force=True)
        _atomic_write_json(current_run_pointer(),
                           {"run_id": run_id, "pid": os.getpid(), "run_dir": str(run_dir)})

    def write(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_write < 0.25:
            return
        self._last_write = now
        _atomic_write_json(self.path, self.state)

    def _path_state(self, profile: str, rel: str) -> dict:
        prof = self.state["profiles"][profile]
        prof["status"] = "running"
        return prof["paths"].setdefault(rel, {})

    def path_phase(self, profile: str, rel: str, phase: str, **extra) -> None:
        ps = self._path_state(profile, rel)
        ps["phase"] = phase
        ps.pop("leg", None)
        ps.update(extra)
        self.write(force=True)

    def leg_progress(self, profile: str, rel: str, leg: str,
                     bytes_done: int, pct, files_done, files_total) -> None:
        ps = self._path_state(profile, rel)
        ps["leg"] = {"name": leg, "bytes": bytes_done, "pct": pct,
                     "files_done": files_done, "files_total": files_total}
        self.write()

    def path_done(self, profile: str, rel: str, summary: dict) -> None:
        ps = self._path_state(profile, rel)
        ps.clear()
        ps["phase"] = "done"
        ps.update(summary)
        self.write(force=True)

    def profile_done(self, profile: str, status: str = "done") -> None:
        self.state["profiles"][profile]["status"] = status
        self.write(force=True)

    def error(self, message: str) -> None:
        self.state["errors"].append(message)
        self.write(force=True)

    def finish(self, status: str) -> None:
        self.state["status"] = status
        self.state["finished_ts"] = time.time()
        self.write(force=True)


@dataclass
class Peer:
    host: str
    user: str | None = None
    home: str | None = None

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host


@dataclass
class Profile:
    name: str
    paths: list[str]
    conflict: str = "newer"
    recursive: bool = True
    exclude: list[str] = field(default_factory=list)
    rename_min_size: int = 64
    workers: int = 8


def load_config(path: str | None) -> tuple[Peer, dict[str, Profile], dict[str, Any]]:
    """Parse sync-profiles.yaml into (peer, profiles, defaults)."""
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - environment issue
        raise HomesyncError("fsync sync requires PyYAML (pip install pyyaml)") from e

    cfg_path = Path(path or DEFAULT_CONFIG).expanduser()
    if not cfg_path.exists():
        raise HomesyncError(f"no profiles config at {cfg_path} — run `fsync sync init` first")
    data = yaml.safe_load(cfg_path.read_text()) or {}

    peer_raw = data.get("peer") or {}
    if not peer_raw.get("host"):
        raise HomesyncError(f"{cfg_path}: peer.host is required")
    peer = Peer(host=peer_raw["host"], user=peer_raw.get("user"), home=peer_raw.get("home"))

    defaults = data.get("defaults") or {}
    profiles: dict[str, Profile] = {}
    for name, raw in (data.get("profiles") or {}).items():
        raw = raw or {}
        paths = raw.get("paths")
        if not paths or not isinstance(paths, list):
            raise HomesyncError(f"{cfg_path}: profile '{name}' needs a non-empty paths list")
        for p in paths:
            if os.path.isabs(str(p)):
                raise HomesyncError(f"{cfg_path}: profile '{name}': paths must be relative to home, got {p}")
        conflict = raw.get("conflict", defaults.get("conflict", "newer"))
        if conflict not in ("newer", "review", "a-wins", "b-wins"):
            raise HomesyncError(f"{cfg_path}: profile '{name}': unknown conflict policy {conflict!r}")
        profiles[name] = Profile(
            name=name,
            paths=[str(p) for p in paths],
            conflict=conflict,
            recursive=bool(raw.get("recursive", True)),
            exclude=list(defaults.get("exclude", [])) + list(raw.get("exclude", [])),
            rename_min_size=int(raw.get("rename_min_size", defaults.get("rename_min_size", 64))),
            workers=int(raw.get("workers", defaults.get("workers", 8))),
        )
    if not profiles:
        raise HomesyncError(f"{cfg_path}: no profiles defined")
    return peer, profiles, defaults


# --------------------------------------------------------------------------- #
# peer plumbing                                                               #
# --------------------------------------------------------------------------- #

def _ssh(peer: Peer, command: str, *, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", *SSH_OPTS, peer.target, command],
        capture_output=True, text=True, timeout=timeout,
    )


def peer_reachable(peer: Peer) -> bool:
    try:
        return _ssh(peer, "true", timeout=15).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def peer_home(peer: Peer) -> str:
    """The peer's home dir: from config, else asked once over SSH."""
    if peer.home:
        return peer.home.rstrip("/")
    proc = _ssh(peer, 'printf %s "$HOME"', timeout=15)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise HomesyncError(f"cannot determine peer home on {peer.target}: {proc.stderr.strip()}")
    peer.home = proc.stdout.strip()
    return peer.home


def ensure_peer_engine(peer: Peer) -> None:
    """Push this box's fsync package to the peer so it can index locally.

    The peer needs nothing pre-installed beyond python3: ``fsync index`` only
    imports the standard library, and the engine copy is refreshed (delta) on
    every run so both sides always execute the same code.
    """
    pkg_dir = Path(__file__).resolve().parent
    mk = _ssh(peer, f"mkdir -p {shlex.quote(ENGINE_DIR)}/fsync", timeout=30)
    if mk.returncode != 0:
        raise HomesyncError(f"peer engine dir creation failed: {mk.stderr.strip()}")
    proc = subprocess.run(
        ["rsync", "-a", "--delete", "--exclude", "__pycache__",
         "-e", "ssh " + " ".join(SSH_OPTS),
         f"{pkg_dir}/", f"{peer.target}:{ENGINE_DIR}/fsync/"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise HomesyncError(f"pushing engine to {peer.target} failed: {proc.stderr.strip()}")


def remote_index(
    peer: Peer,
    root: str,
    *,
    recursive: bool,
    exclude: list[str],
    hash_algo: str = "sha256",
    workers: int = 8,
) -> list[dict]:
    """Index ``root`` on the peer and return its records (JSONL over stdout)."""
    if _ssh(peer, f"test -d {shlex.quote(root)}", timeout=30).returncode != 0:
        return []  # absent on the peer -> everything local flows over as additive
    parts = [
        f"PYTHONPATH=$HOME/{ENGINE_DIR}",
        "python3", "-m", "fsync.cli", "index", shlex.quote(root),
        "--format", "jsonl", "--cache", "--workers", str(workers), "--hash", hash_algo,
    ]
    if not recursive:
        parts.append("--no-recursive")
    for pat in exclude:
        parts += ["--exclude", shlex.quote(pat)]
    proc = _ssh(peer, " ".join(parts))
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-4:])
        raise HomesyncError(f"remote index of {root} failed (rc={proc.returncode}): {tail}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def local_index(
    root: Path,
    *,
    recursive: bool,
    exclude: list[str],
    hash_algo: str = "sha256",
    workers: int = 8,
) -> list[dict]:
    if not root.is_dir():
        return []
    return list_files_with_metadata(
        root,
        recursive=recursive,
        hash_algo=hash_algo,
        workers=workers,
        exclude=exclude or None,
        cache_path=default_cache_path(root),
    )


# --------------------------------------------------------------------------- #
# execution                                                                   #
# --------------------------------------------------------------------------- #

_STATS_FILES = re.compile(r"Number of regular files transferred: ([\d,]+)")
_STATS_BYTES = re.compile(r"Total transferred file size: ([\d,]+)")
# --info=progress2 line: "  1,234,567  45%   10.25MB/s  0:00:12 (xfr#5, ir-chk=10/200)"
_PROGRESS2 = re.compile(
    rb"([\d,]+)\s+(\d+)%\s+\S+/s\s+[\d:]+(?:\s+\(xfr#(\d+), (?:ir|to)-chk=(\d+)/(\d+)\))?"
)


def _stat(pattern: re.Pattern, text: str) -> int:
    m = pattern.search(text)
    return int(m.group(1).replace(",", "")) if m else 0


def parse_progress_chunk(chunk: bytes):
    """Extract (bytes, pct, files_done, files_total) from the newest
    --info=progress2 line in a raw stdout chunk, or None."""
    last = None
    for m in _PROGRESS2.finditer(chunk):
        last = m
    if last is None:
        return None
    done = int(last.group(1).replace(b",", b""))
    pct = int(last.group(2))
    files_done = int(last.group(3)) if last.group(3) else None
    total = int(last.group(5)) if last.group(5) else None
    return done, pct, files_done, total


def run_rsync_leg(
    paths: list[str],
    src: str,
    dest: str,
    backup_dir: str,
    lst_path: Path,
    *,
    dry_run: bool,
    overwrites: int,
    on_progress=None,
) -> dict[str, Any]:
    """One directional transfer driven by --files-from, with backup-dir safety.

    stdout is streamed so ``on_progress(bytes, pct, files_done, files_total)``
    can feed a live UI; stderr spools to ``<lst>.err`` (a second pipe could
    deadlock while we read stdout)."""
    leg: dict[str, Any] = {"files_planned": len(paths), "overwrites_expected": overwrites,
                           "files_transferred": 0, "bytes": 0, "rc": 0, "warnings": []}
    if not paths:
        return leg
    lst_path.parent.mkdir(parents=True, exist_ok=True)
    lst_path.write_text("\n".join(paths) + "\n")
    cmd = [
        "rsync", "-aHAXS", "--numeric-ids", "--ignore-times", "--partial", "--stats",
        "--info=progress2",
        "--backup", f"--backup-dir={backup_dir}",
        "-e", DEFAULT_RSYNC_SSH + " -o BatchMode=yes",
        f"--files-from={lst_path}",
    ]
    if dry_run:
        cmd.append("-n")
    cmd += [src.rstrip("/") + "/", dest.rstrip("/") + "/"]

    err_path = lst_path.with_suffix(lst_path.suffix + ".err")
    chunks: list[bytes] = []
    with open(err_path, "wb") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                stdin=subprocess.DEVNULL)
        fd = proc.stdout.fileno()
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            chunks.append(data)
            if on_progress:
                parsed = parse_progress_chunk(data)
                if parsed:
                    on_progress(*parsed)
        proc.wait()

    stdout_text = b"".join(chunks).decode(errors="replace")
    stderr_text = err_path.read_text(errors="replace")
    leg["rc"] = proc.returncode
    leg["files_transferred"] = _stat(_STATS_FILES, stdout_text)
    leg["bytes"] = _stat(_STATS_BYTES, stdout_text)
    if proc.returncode not in TOLERATED_RSYNC_EXITS:
        raise HomesyncError(
            f"rsync {src} -> {dest} failed (rc={proc.returncode}): "
            + "\n".join(stderr_text.strip().splitlines()[-4:])
        )
    if proc.returncode != 0:
        leg["warnings"] = [ln for ln in stderr_text.splitlines() if ln.strip()][-8:]
    return leg


def _plan_path(peer: Peer, prof: Profile, rel: str, n_workers: int):
    """Index both sides of one profile path and build its sync plan.

    Shared by the executing run and the plan-only preview so they can never
    disagree on semantics. Returns (local_root, remote_root, plan, changed)
    where ``changed`` is the set of same-path/different-content paths — the
    ones whose transfer overwrites (and therefore backs up) a file.
    """
    home = Path.home()
    p_home = peer_home(peer)
    local_root = home if rel == "." else home / rel
    remote_root = p_home if rel == "." else f"{p_home}/{rel}"
    idx_a = local_index(local_root, recursive=prof.recursive, exclude=prof.exclude, workers=n_workers)
    idx_b = remote_index(peer, remote_root, recursive=prof.recursive, exclude=prof.exclude, workers=n_workers)
    report = compare_file_lists(idx_a, idx_b, match_on="path")
    plan = build_sync_plan(report, conflict=prof.conflict, rename_min_size=prof.rename_min_size)
    changed = {pair[0].get("path") for pair in report["name_matches_diff_hash"]}
    return local_root, remote_root, plan, changed


def build_run_preview(
    peer: Peer,
    selected: list[Profile],
    workers: int | None = None,
    sample_n: int = 8,
) -> dict[str, Any]:
    """Plan every selected profile without transferring: 'what is coming'.

    The executing run re-plans from fresh indexes, so these numbers are a
    preview — live trees can drift between confirm and execute; the run
    report is authoritative."""
    out: dict[str, Any] = {"profiles": {}}
    for prof in selected:
        pp: dict[str, Any] = {"conflict": prof.conflict, "paths": {}}
        for rel in prof.paths:
            _, _, plan, changed = _plan_path(peer, prof, rel, workers or prof.workers)
            a_items = [it for it in plan["a_to_b"] if it.get("path")]
            b_items = [it for it in plan["b_to_a"] if it.get("path")]
            pp["paths"][rel] = {
                "a_to_b": len(a_items),
                "b_to_a": len(b_items),
                "bytes_a_to_b": sum(it.get("size") or 0 for it in a_items),
                "bytes_b_to_a": sum(it.get("size") or 0 for it in b_items),
                "overwrites_a_to_b": sum(1 for it in a_items if it["path"] in changed),
                "overwrites_b_to_a": sum(1 for it in b_items if it["path"] in changed),
                "conflicts": len(plan["conflicts"]),
                "identical": plan["noop"],
                "renames_demoted": len(plan["renames_suppressed"]),
                "samples": {
                    "a_to_b": [it["path"] for it in a_items[:sample_n]],
                    "b_to_a": [it["path"] for it in b_items[:sample_n]],
                },
            }
        out["profiles"][prof.name] = pp
    return out


def run_profile(
    peer: Peer,
    prof: Profile,
    *,
    run_dir: Path,
    run_id: str,
    dry_run: bool,
    workers: int | None,
    log,
    progress: ProgressWriter | None = None,
) -> dict[str, Any]:
    """Index both sides, plan, and execute every path of one profile."""
    p_home = peer_home(peer)
    n_workers = workers or prof.workers
    local_backup = str(Path("~/.fsync/backups").expanduser() / run_id / prof.name)
    remote_backup = f"{p_home}/.fsync/backups/{run_id}/{prof.name}"

    result: dict[str, Any] = {"conflict": prof.conflict, "paths": {}, "status": "ok"}
    for rel in prof.paths:
        started = time.time()
        key = "home" if rel == "." else rel.replace("/", "_")

        if progress:
            progress.path_phase(prof.name, rel, "indexing")
        local_root, remote_root, plan, changed_paths = _plan_path(peer, prof, rel, n_workers)

        # Overwrite = a routed same-path/different-content file; exactly those
        # produce a backup on the receiver. Additive copies overwrite nothing.
        a_paths = [it["path"] for it in plan["a_to_b"] if it.get("path")]
        b_paths = [it["path"] for it in plan["b_to_a"] if it.get("path")]
        over_ab = sum(1 for p in a_paths if p in changed_paths)
        over_ba = sum(1 for p in b_paths if p in changed_paths)

        if progress:
            progress.path_phase(prof.name, rel, "a_to_b", planned=len(a_paths))
        leg_ab = run_rsync_leg(
            a_paths, str(local_root), f"{peer.target}:{remote_root}", remote_backup,
            run_dir / prof.name / f"{key}.a_to_b.lst", dry_run=dry_run, overwrites=over_ab,
            on_progress=(lambda b, p, fd, ft: progress.leg_progress(prof.name, rel, "a_to_b", b, p, fd, ft))
            if progress else None,
        )
        if progress:
            progress.path_phase(prof.name, rel, "b_to_a", planned=len(b_paths))
        leg_ba = run_rsync_leg(
            b_paths, f"{peer.target}:{remote_root}", str(local_root), local_backup,
            run_dir / prof.name / f"{key}.b_to_a.lst", dry_run=dry_run, overwrites=over_ba,
            on_progress=(lambda b, p, fd, ft: progress.leg_progress(prof.name, rel, "b_to_a", b, p, fd, ft))
            if progress else None,
        )

        conflicts = [
            {"path": a.get("path"),
             "a": {"mtime": a.get("mtime"), "hash": a.get("hash"), "size": a.get("size")},
             "b": {"mtime": b.get("mtime"), "hash": b.get("hash"), "size": b.get("size")}}
            for a, b in plan["conflicts"]
        ]
        if conflicts:
            cf = run_dir / prof.name / f"{key}.conflicts.json"
            cf.parent.mkdir(parents=True, exist_ok=True)
            cf.write_text(json.dumps(conflicts, indent=2))

        result["paths"][rel] = {
            "a_to_b": leg_ab,
            "b_to_a": leg_ba,
            "conflicts": len(conflicts),
            "renames_pending": len(plan["renames"]),
            "renames_demoted": len(plan["renames_suppressed"]),
            "identical": plan["noop"],
            "seconds": round(time.time() - started, 2),
        }
        if progress:
            progress.path_done(prof.name, rel, result["paths"][rel])
        log(
            f"  {prof.name}/{rel}: A->B {len(a_paths)}"
            + (f" ({over_ab} overwrite->backup)" if over_ab else "")
            + f", B->A {len(b_paths)}"
            + (f" ({over_ba} overwrite->backup)" if over_ba else "")
            + f", conflicts {len(conflicts)}, identical {plan['noop']}"
            + f" [{result['paths'][rel]['seconds']}s]"
        )
    return result


# --------------------------------------------------------------------------- #
# CLI entry                                                                   #
# --------------------------------------------------------------------------- #

def _cmd_init(args) -> int:
    cfg_path = Path(getattr(args, "config", None) or DEFAULT_CONFIG).expanduser()
    if cfg_path.exists() and not args.force:
        print(f"{cfg_path} already exists (use --force to overwrite)", file=sys.stderr)
        return 2
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(DEFAULT_PROFILES_YAML)
    print(f"wrote starter profiles to {cfg_path} — edit peer/profiles, then: fsync sync run --all")
    return 0


def _cmd_run(args) -> int:
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    try:
        peer, profiles, _defaults = load_config(getattr(args, "config", None))
    except HomesyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.all:
        selected = list(profiles.values())
    elif args.profile:
        missing = [n for n in args.profile if n not in profiles]
        if missing:
            print(f"unknown profile(s): {', '.join(missing)} (available: {', '.join(profiles)})", file=sys.stderr)
            return 2
        selected = [profiles[n] for n in args.profile]
    else:
        print(f"pick profiles with --profile NAME (repeatable) or --all; available: {', '.join(profiles)}", file=sys.stderr)
        return 2

    # R6: away-tolerant — a missing peer is a clean no-op, not an error.
    if not peer_reachable(peer):
        if getattr(args, "plan_only", False):
            print(json.dumps({"peer_reachable": False, "peer": peer.target}))
            return 0
        print(f"peer {peer.target} not reachable on the LAN — nothing to do", file=sys.stderr)
        return 0

    if getattr(args, "plan_only", False):
        # Preview for a UI: JSON on stdout, no lock, nothing transferred.
        try:
            ensure_peer_engine(peer)
            preview = build_run_preview(peer, selected, workers=args.workers)
        except HomesyncError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        preview["peer_reachable"] = True
        preview["peer"] = peer.target
        print(json.dumps(preview))
        return 0

    st_root = state_root()
    st_root.mkdir(parents=True, exist_ok=True)
    lock_file = (st_root / "sync.lock").open("w")
    import fcntl

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another `fsync sync run` is already in progress — aborting", file=sys.stderr)
        return 2

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    run_dir = st_root / "runs" / run_id
    run_dir.mkdir(parents=True)
    report: dict[str, Any] = {
        "run_id": run_id,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dry_run": bool(args.dry_run),
        "peer": {"host": peer.host, "user": peer.user},
        "profiles": {},
        "errors": [],
    }
    log(f"sync run {run_id}{' (DRY RUN)' if args.dry_run else ''} -> {peer.target}")
    progress = ProgressWriter(run_dir, run_id, bool(args.dry_run), [p.name for p in selected])

    rc = 0
    try:
        ensure_peer_engine(peer)
    except HomesyncError as e:
        progress.error(str(e))
        progress.finish("error")
        print(f"error: {e}", file=sys.stderr)
        return 1

    for prof in selected:
        try:
            report["profiles"][prof.name] = run_profile(
                peer, prof, run_dir=run_dir, run_id=run_id,
                dry_run=args.dry_run, workers=args.workers, log=log, progress=progress,
            )
            progress.profile_done(prof.name)
        except HomesyncError as e:
            report["profiles"].setdefault(prof.name, {})["status"] = "error"
            report["errors"].append(f"{prof.name}: {e}")
            progress.error(f"{prof.name}: {e}")
            progress.profile_done(prof.name, status="error")
            log(f"  {prof.name}: ERROR {e}")
            rc = 1

    report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (run_dir / "report.json").write_text(json.dumps(report, indent=2))
    (st_root / "runs" / "latest").write_text(run_id)
    progress.finish("error" if rc else "done")

    conflicts = sum(
        pr.get("conflicts", 0)
        for prof_r in report["profiles"].values()
        for pr in (prof_r.get("paths") or {}).values()
    )
    log(f"report: {run_dir / 'report.json'}"
        + (f" — {conflicts} conflict(s) held for review" if conflicts else ""))
    return rc


def cmd_sync(args) -> int:
    if getattr(args, "sync_cmd", None) == "init":
        return _cmd_init(args)
    if getattr(args, "sync_cmd", None) == "run":
        return _cmd_run(args)
    print("usage: fsync sync {run|init} ...", file=sys.stderr)
    return 2
