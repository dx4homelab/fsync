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
import socket
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import FEATURES, __version__ as FSYNC_VERSION
from . import git_repo_sync as grs
from . import svnsync as svs
from . import threeway as tw
from .fileindex import (
    DEFAULT_RSYNC_SSH,
    build_sync_plan,
    compare_file_lists,
    default_cache_path,
    list_files_with_metadata,
    matches_exclude,
)

DEFAULT_CONFIG = "~/.config/fsync/sync-profiles.yaml"
STATE_ROOT = "~/.local/state/fsync"
ENGINE_DIR = ".local/lib/fsync-engine"  # relative to the peer's $HOME
DEFAULT_GIT_BUNDLE_DIR = "~/.fsync/git-bundles"  # P5: kind=git bundles land here
DEFAULT_GIT_BACKUP_ROOT = "~/.fsync/backups"     # pre-apply receiver backups
DEFAULT_SVN_SNAPSHOT_DIR = "~/.fsync/svn-snapshots"  # ISSUE-010: kind=svn snapshots land here
STRANDED_SAMPLE = 20  # ISSUE-003: how many withheld paths to name in the report
RESURRECTED_SAMPLE = 20  # ISSUE-010 fix 3: resurrected paths named inline (full list in a .lst)

# StrictHostKeyChecking=accept-new (not =yes): the SYNC channel is ssh, which
# verifies host keys against known_hosts. Off-LAN, a new address (the peer's
# tailnet name/IP) has no pinned key yet, and =yes would refuse it in
# BatchMode → the run silently no-ops, defeating P4.4. accept-new pins an
# UNKNOWN host on first connect (safe: the tailnet is WireGuard-authenticated)
# but still REFUSES a CHANGED key (a real MITM). The cert-pinned mTLS API path
# is separate and host-key-independent.
_STRICT = "-o", "StrictHostKeyChecking=accept-new"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", *_STRICT]
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
# direction: both|push|pull = per-profile default; push sends local->peer
#                    only (peer-newer files are SKIPPED, never clobbered),
#                    pull is the reverse. The TUI digit keys cycle this per
#                    run: ⇅ both -> → push -> ← pull -> · off.
peer:
  host: minis4dx.lan   # router DNS name; bare hostname resolution is not reliable here
  user: developer
  home: /var/home/developer
  # addresses: ordered ways to reach the peer, first ssh-reachable wins. LAN
  # first, then an off-LAN route (Tailscale) for when the laptop is away. Add
  # the peer's tailnet name after `tailscale up` on both boxes. Defaults to
  # [host] when omitted. Peer identity is the pinned cert (P4.1), so the API
  # can be reached by ANY of these addresses without SAN juggling.
  addresses: [minis4dx.lan]   # e.g. [minis4dx.lan, minis4dx.tailXXXX.ts.net]
daemon:
  # fsyncd (P4): loopback token-TLS on 127.0.0.1:<port>, plus mTLS for peers
  # on <bind>:<mtls_port> (mtls_port defaults to port+2). bind=all (0.0.0.0)
  # is the default and is safe — mTLS requires a pinned client cert, so an
  # unpinned caller can't connect; that is what makes off-LAN (Tailscale/any
  # route) work. The daemon schedules runs itself.
  port: 7444
  mtls_port: 7446
  bind: all
  interval: 1h
  jitter: 240s
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
  homelab:
    # Workspace repos travel as plain files (.git included) — no git remotes
    # involved. Bidirectional newest-wins is safe while one box at a time is
    # active in a repo; overwrites are backed up like everything else.
    paths: [workspaces/homelab]
    # "mtab": extracted OS rootfs trees symlink etc/mtab -> /proc/mounts,
    # whose mtime is always "now" — syncing it ping-pongs every run.
    # ".svn": a working copy's metadata (wc.db, pristine bases) is box-local —
    # pasting one box's onto the other's tree corrupts it (ISSUE-004/010).
    exclude: [".svn", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
              "*.pyc", "backups", "mtab", "fsync/scratchpad/repo-sync/*"]
  primary:
    # Client workspace. .metadata is Eclipse runtime state (locks, caches,
    # per-machine UI state) — never sync it.
    paths: [workspaces/primary]
    exclude: [".svn", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
              "*.pyc", "backups", "mtab", ".metadata/*"]
  claude:
    # Durable artifacts only (memories, agents, skills, plugins). Session
    # ephemera (transcripts, file-history, todos, plans, caches) are managed
    # by Claude Code's own ~30-day GC: syncing them just ping-pongs — the
    # receiver's next cleanup deletes them and the next sync re-copies them.
    paths: [.claude]
    conflict: review
    # history.jsonl is append-only JSONL that diverges whenever both boxes
    # are used; whole-file resolution can only clobber a side. Union-merge
    # dedupes lines and interleaves by timestamp instead.
    merge_jsonl: ["history.jsonl"]
    exclude: [
      ".credentials.json", "backups/*", "statsig/*", "shell-snapshots/*",
      "file-history/*", "projects/*.jsonl", "todos/*", "tasks/*", "plans/*",
      "cache/*", "debug/*", "session-env/*",
      # per-machine noise (2026-07-03 triage): live-session records, caches,
      # updater/GC state, approval residue, self-refreshing plugin registry.
      # What remains reviewable on purpose: settings.json and memory/.
      "sessions/*", "stats-cache.json", ".last-update-result.json",
      ".last-cleanup", "settings.local.json", "plugins/*",
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


def union_merge_jsonl(a_text: str, b_text: str) -> str:
    """Line-union of two append-only JSONL files.

    Exact duplicate lines collapse; entries are ordered by their `timestamp`
    field (Claude Code's history.jsonl carries ms-epoch timestamps on every
    line), so both boxes' entries interleave chronologically. Lines without
    a parseable timestamp are preserved after the timestamped ones in
    first-seen order. Merging the merge with either input is a no-op, so
    repeated runs converge.
    """
    seen: dict[str, Any] = {}
    for text in (a_text, b_text):
        for line in text.splitlines():
            if line.strip() and line not in seen:
                try:
                    ts = json.loads(line).get("timestamp")
                except ValueError:
                    ts = None
                seen[line] = ts if isinstance(ts, (int, float)) else None
    timed = sorted(
        ((ts, i, ln) for i, (ln, ts) in enumerate(seen.items()) if ts is not None)
    )
    untimed = [ln for ln, ts in seen.items() if ts is None]
    merged = [ln for _, _, ln in timed] + untimed
    return "\n".join(merged) + "\n" if merged else ""


def report_totals(report: dict) -> tuple[int, int, int]:
    """(files_moved, conflicts_held, errors) across a run report."""
    moved = conflicts = 0
    for prof_r in (report.get("profiles") or {}).values():
        for pr in (prof_r.get("paths") or {}).values():
            conflicts += pr.get("conflicts", 0)
            for leg in (pr.get("a_to_b", {}), pr.get("b_to_a", {})):
                moved += leg.get("files_transferred", 0)
    return moved, conflicts, len(report.get("errors") or [])


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
    addresses: list[str] = field(default_factory=list)  # ordered; first = host
    _active: str | None = None   # the address a recent probe found reachable
    # P7.1 hub-sync: the counterpart lives on THIS box (the hub box runs a
    # spoke against its own hub tree) — every "remote" operation is a plain
    # local command / local rsync; ssh and the engine push are skipped.
    local: bool = False

    def all_addresses(self) -> list[str]:
        return self.addresses or [self.host]

    def spec(self, path: str) -> str:
        """rsync endpoint for ``path`` on the counterpart: ``user@host:path``,
        or the bare path when the counterpart is this very box."""
        return path if self.local else f"{self.target}:{path}"

    @property
    def label(self) -> str:
        return self.target

    @property
    def active_host(self) -> str:
        """The address to actually use — the last one a probe reached, else host."""
        return self._active or self.host

    @property
    def target(self) -> str:
        h = self.active_host
        return f"{self.user}@{h}" if self.user else h


@dataclass
class Hub(Peer):
    """The star topology's centre (docs/hub-sync.md R30/R31): a Peer whose
    remote roots are the hub TREE (``<root>/<profile>/tree``), not a live home,
    plus this box's roles. ``local`` is True on the hub box itself, which runs
    an ordinary spoke run against its own tree (local rsync, no ssh)."""
    root: str = "~/.fsync/hub"
    roles: list[str] = field(default_factory=lambda: ["spoke"])  # THIS box's roles
    retention: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"hub {self.host}" + (" (this box)" if self.local else f" via {self.target}")


HUB_ROLES = ("hub", "spoke")

DIRECTIONS = ("both", "push", "pull")  # push = local->peer only, pull = peer->local only


@dataclass
class Profile:
    name: str
    paths: list[str]
    conflict: str = "newer"
    recursive: bool = True
    exclude: list[str] = field(default_factory=list)
    rename_min_size: int = 64
    workers: int = 8
    direction: str = "both"
    merge_jsonl: list[str] = field(default_factory=list)
    # P5 git-repo-sync: kind="git" profiles carry full git working state via
    # bundles instead of additive rsync (docs/git-repo-sync.md).
    # ISSUE-010: kind="svn" mirrors it for Subversion working copies — URL +
    # revision + diff + unversioned files travel as a snapshot; committed
    # history travels through the SVN server (fsync/svnsync.py).
    kind: str = "files"
    mirror_branches: bool = False
    # ISSUE-001 fix C: kind:git receiver behaviour. "on-demand" (default, R17 v1)
    # = apply manually with `fsync git apply`, and file profiles keep carrying the
    # repos (double-coverage). "auto" = the durable fix: file profiles SKIP these
    # repos, and the receiver auto-applies bundles in the run, guarded (clean +
    # same-branch + not-busy; else skip). Realizes R17's reserved guarded mode.
    apply: str = "on-demand"
    # P7.2 hub-sync: "full" = three-way vs base + tombstones + journal through the
    # hub (deletes propagate, reversibly); "additive" = the P7.1 behaviour (never
    # removes anything). v1 pairwise configs are always additive.
    mode: str = "additive"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into ``base`` (override wins; lists replaced)."""
    out = dict(base)
    for k, v in (override or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _resolve_config(cfg_path: Path, yaml) -> dict:
    """Load the config, merging any `include:` files UNDER it (meta-sync: the
    included file holds the SHARED profile definitions synced between boxes; the
    box-local main file overlays peer/direction and wins on conflicts)."""
    main = yaml.safe_load(cfg_path.read_text()) or {}
    includes = main.get("include") or []
    if not includes:
        return main
    merged: dict = {}
    for inc in includes:
        inc_path = Path(os.path.expanduser(str(inc)))
        if not inc_path.is_absolute():
            inc_path = cfg_path.parent / inc_path
        if not inc_path.exists():
            raise HomesyncError(f"{cfg_path}: include not found: {inc_path}")
        merged = _deep_merge(merged, yaml.safe_load(inc_path.read_text()) or {})
    return _deep_merge(merged, {k: v for k, v in main.items() if k != "include"})


def _match_host(hosts: dict, hostname: str) -> str | None:
    """Match a hostname to a hosts: key — exact, else short-name in BOTH
    directions ('fury4dx' matches gethostname 'fury4dx.lan' and a config key
    'fury4dx.lan' matches gethostname 'fury4dx')."""
    if hostname in hosts:
        return hostname
    short = hostname.split(".")[0]
    if short in hosts:
        return short
    for hk in hosts:
        if hk.split(".")[0] == short:
            return hk
    return None


def _select_host_config(data: dict, hostname: str) -> dict:
    """Collapse a multi-host config (P6: top-level `hosts:` + `shared:`) to this
    box's active single-host config. `hosts[H]` (peer, direction, ssh) overlays
    `shared`; `requires` carries from the top. Unknown host is a clear error."""
    hosts = data.get("hosts") or {}
    key = _match_host(hosts, hostname)
    if not key:
        raise HomesyncError(
            f"host '{hostname}' is not in the config hosts {sorted(hosts)} — "
            f"add a hosts: entry (see docs/meta-deploy.md)")
    merged = _deep_merge(data.get("shared") or {}, hosts[key] or {})
    if data.get("requires"):
        merged["requires"] = data["requires"]
    # P7.1 (config v2): the hub block is global, the role is per host (already
    # merged in from hosts[key]); both ride along to the v2 loader.
    for k in ("hub", "version"):
        if k in data and k not in merged:
            merged[k] = data[k]
    return merged


def _split_ssh_target(target: str | None) -> tuple[str | None, str | None]:
    """'user@host' -> (user, host); 'host' -> (None, host); None -> (None, None)."""
    if not target:
        return None, None
    user, sep, host = str(target).rpartition("@")
    return (user or None) if sep else None, host or None


def _build_hub(data: dict, cfg_path: Path, hostname: str) -> Hub:
    """Config v2 (docs/hub-sync.md): one ``hub:`` block + ``role:`` per host.
    The hub box must declare the hub role and vice versa — a mismatch is a
    config error, never a guess."""
    raw = data.get("hub")
    if not isinstance(raw, dict) or not raw.get("host"):
        raise HomesyncError(f"{cfg_path}: version 2 config needs a `hub:` block with `host`")
    host = str(raw["host"])
    user, ssh_host = _split_ssh_target(raw.get("ssh"))
    addrs = raw.get("addresses") or [ssh_host or host]
    if not isinstance(addrs, list) or not all(isinstance(a, str) and a for a in addrs):
        raise HomesyncError(f"{cfg_path}: hub.addresses must be a list of non-empty strings")
    roles = data.get("role") or ["spoke"]
    if isinstance(roles, str):
        roles = [roles]
    if not isinstance(roles, list) or not roles or any(r not in HUB_ROLES for r in roles):
        raise HomesyncError(f"{cfg_path}: role must be a non-empty subset of {HUB_ROLES}, got {roles!r}")
    is_hub_box = _match_host({host: True}, hostname) is not None
    if ("hub" in roles) != is_hub_box:
        raise HomesyncError(
            f"{cfg_path}: hub.host is {host!r} but this box ({hostname}) "
            + ("declares the hub role" if "hub" in roles else "does not declare role: [hub, ...]"))
    root = str(raw.get("root") or "~/.fsync/hub")
    retention = raw.get("retention") or {}
    if not isinstance(retention, dict):
        raise HomesyncError(f"{cfg_path}: hub.retention must be a mapping")
    return Hub(host=host, user=user, home=raw.get("home"), addresses=[str(a) for a in addrs],
               local="hub" in roles, root=root, roles=[str(r) for r in roles],
               retention=dict(retention))


def load_config(path: str | None) -> tuple[Peer, dict[str, Profile], dict[str, Any]]:
    """Parse sync-profiles.yaml into (peer, profiles, defaults)."""
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - environment issue
        raise HomesyncError("fsync sync requires PyYAML (pip install pyyaml)") from e

    cfg_path = Path(path or DEFAULT_CONFIG).expanduser()
    if not cfg_path.exists():
        raise HomesyncError(f"no profiles config at {cfg_path} — run `fsync sync init` first")
    data = _resolve_config(cfg_path, yaml)
    # P6 multi-host: a config with a top-level `hosts:` self-selects by hostname.
    if "hosts" in data:
        data = _select_host_config(data, socket.gethostname())

    # meta-sync compatibility gate: refuse config that needs capabilities this
    # build lacks, rather than misbehaving (config may reach a box before code).
    missing = [f for f in (data.get("requires") or []) if f not in FEATURES]
    if missing:
        raise HomesyncError(
            f"{cfg_path}: requires fsync feature(s) {missing} not in this build "
            f"{sorted(FEATURES)} — upgrade fsync on this box (meta-sync skew)")

    peer: Peer
    try:
        version = int(data.get("version") or 1)
    except (TypeError, ValueError):
        raise HomesyncError(f"{cfg_path}: version must be an integer") from None
    if version >= 2 or data.get("hub") is not None:
        # P7.1 star topology: the counterpart of every box is the hub.
        peer = _build_hub(data, cfg_path, socket.gethostname())
    else:
        peer_raw = data.get("peer") or {}
        if not peer_raw.get("host"):
            raise HomesyncError(f"{cfg_path}: peer.host is required")
        addrs = peer_raw.get("addresses") or [peer_raw["host"]]
        if not isinstance(addrs, list) or not all(isinstance(a, str) and a for a in addrs):
            raise HomesyncError(f"{cfg_path}: peer.addresses must be a list of non-empty strings")
        peer = Peer(host=peer_raw["host"], user=peer_raw.get("user"), home=peer_raw.get("home"),
                    addresses=[str(a) for a in addrs])

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
        direction = raw.get("direction", defaults.get("direction", "both"))
        if direction not in DIRECTIONS:
            raise HomesyncError(f"{cfg_path}: profile '{name}': direction must be one of {DIRECTIONS}")
        kind = raw.get("kind", "files")
        if kind not in ("files", "git", "svn"):
            raise HomesyncError(f"{cfg_path}: profile '{name}': kind must be 'files', 'git' or 'svn', got {kind!r}")
        apply_mode = raw.get("apply", defaults.get("apply", "on-demand"))
        if apply_mode not in ("on-demand", "auto"):
            raise HomesyncError(f"{cfg_path}: profile '{name}': apply must be 'on-demand' or 'auto', got {apply_mode!r}")
        mode = raw.get("mode", defaults.get("mode", "full" if isinstance(peer, Hub) else "additive"))
        if mode not in ("full", "additive"):
            raise HomesyncError(f"{cfg_path}: profile '{name}': mode must be 'full' or 'additive', got {mode!r}")
        if mode == "full" and not isinstance(peer, Hub):
            raise HomesyncError(f"{cfg_path}: profile '{name}': mode: full needs a hub (config v2)")
        profiles[name] = Profile(
            name=name,
            paths=[str(p) for p in paths],
            conflict=conflict,
            recursive=bool(raw.get("recursive", True)),
            exclude=list(defaults.get("exclude", [])) + list(raw.get("exclude", [])),
            rename_min_size=int(raw.get("rename_min_size", defaults.get("rename_min_size", 64))),
            workers=int(raw.get("workers", defaults.get("workers", 8))),
            direction=direction,
            merge_jsonl=[str(p) for p in raw.get("merge_jsonl", defaults.get("merge_jsonl", []))],
            kind=kind,
            mirror_branches=bool(raw.get("mirror_branches", False)),
            apply=apply_mode,
            mode=mode,
        )
    if not profiles:
        raise HomesyncError(f"{cfg_path}: no profiles defined")
    return peer, profiles, defaults


# --------------------------------------------------------------------------- #
# peer plumbing                                                               #
# --------------------------------------------------------------------------- #

def _ssh_to(target: str, command: str, *, timeout: int = 900) -> subprocess.CompletedProcess:
    """Run one command on ``target``. A hung link surfaces as ``HomesyncError``
    (ISSUE-009): a peer going to sleep mid-command used to escape as a raw
    ``subprocess.TimeoutExpired`` and take the whole scheduled run down."""
    try:
        return subprocess.run(
            ["ssh", *SSH_OPTS, target, command],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise HomesyncError(
            f"ssh {target}: no reply within {timeout}s (peer asleep or link down?) "
            f"while running: {command[:80]}{'…' if len(command) > 80 else ''}"
        ) from e


def _local_shell(command: str, *, timeout: int = 900) -> subprocess.CompletedProcess:
    """The local-mode twin of ``_ssh_to``: same command text, same contract
    (``HomesyncError`` on a hang), run by bash on this box."""
    try:
        return subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise HomesyncError(f"local command gave no reply within {timeout}s: "
                            f"{command[:80]}{'…' if len(command) > 80 else ''}") from e


def _ssh(peer: Peer, command: str, *, timeout: int = 900) -> subprocess.CompletedProcess:
    if peer.local:
        return _local_shell(command, timeout=timeout)
    # peer.target follows peer._active, set by the most recent peer_reachable()
    return _ssh_to(peer.target, command, timeout=timeout)


def peer_reachable(peer: Peer) -> bool:
    """Probe the peer's addresses in order; remember the first ssh-reachable one
    on the Peer (so the sync + API then use it). LAN name first, off-LAN
    (Tailscale) fallback next — so a run just works whether home or away."""
    if peer.local:
        peer._active = "127.0.0.1"
        return True
    user = peer.user
    for addr in peer.all_addresses():
        target = f"{user}@{addr}" if user else addr
        try:
            if _ssh_to(target, "true", timeout=10).returncode == 0:
                peer._active = addr
                return True
        except (subprocess.TimeoutExpired, HomesyncError):
            continue
    return False


def peer_busy(peer: Peer) -> bool:
    """True when the peer is mid-run (holds its own sync.lock).

    Both boxes are drivers; two concurrent runs would race each other's
    transfers. flock(1) and Python's fcntl.flock share BSD lock semantics,
    so probing with a non-blocking flock is exact. An ssh failure counts as
    busy — deferring is always the safe answer for a scheduled run.

    A hub is never "busy" as a whole: its per-profile leases (R36) are taken
    inside the run, so one profile in flight elsewhere defers only itself.
    """
    if isinstance(peer, Hub):
        return False
    try:
        probe = _ssh(
            peer,
            "mkdir -p ~/.local/state/fsync && flock -n ~/.local/state/fsync/sync.lock true",
            timeout=20,
        )
        return probe.returncode != 0
    except (subprocess.TimeoutExpired, HomesyncError):
        return True


def _notify(summary: str, body: str) -> None:
    """Best-effort desktop notification; failures never affect the run."""
    try:
        subprocess.run(["notify-send", "--app-name=fsync", summary, body],
                       check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def peer_home(peer: Peer) -> str:
    """The peer's home dir: from config, else asked once over SSH."""
    if peer.home:
        return peer.home.rstrip("/")
    if peer.local:
        peer.home = str(Path.home())
        return peer.home
    proc = _ssh(peer, 'printf %s "$HOME"', timeout=15)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise HomesyncError(f"cannot determine peer home on {peer.target}: {proc.stderr.strip()}")
    peer.home = proc.stdout.strip()
    return peer.home


def _engine_push_args(pkg_dir: Path) -> tuple[list[str], str]:
    """(rsync source args, destination rel-path) for pushing the engine.

    From a source install the package DIRECTORY is pushed (delta, as before).
    From a deployed zipapp ``pkg_dir`` is a zip member, not a directory — push
    the running .pyz itself instead: python imports fsync straight off a zip on
    PYTHONPATH, so one file IS the engine (P6.4 live-test finding)."""
    if pkg_dir.is_dir():
        return (["--delete", "--exclude", "__pycache__", f"{pkg_dir}/"],
                f"{ENGINE_DIR}/fsync/")
    archive = pkg_dir.parent  # .../fsync.pyz/fsync -> the .pyz file
    if not archive.is_file():
        raise HomesyncError(f"cannot locate engine source (pkg at {pkg_dir})")
    return ([str(archive)], f"{ENGINE_DIR}/fsync.pyz")


def ensure_peer_engine(peer: Peer) -> None:
    """Push this box's fsync engine to the peer so it can index locally.

    The peer needs nothing pre-installed beyond python3: ``fsync index`` only
    imports the standard library, and the engine copy is refreshed on every
    run so both sides always execute the same code. The engine is the package
    dir (source install) or the running .pyz (deployed box)."""
    if peer.local:
        return  # the counterpart runs this very code
    src_args, dst = _engine_push_args(Path(__file__).resolve().parent)
    mk = _ssh(peer, f"mkdir -p {shlex.quote(ENGINE_DIR)}/fsync", timeout=30)
    if mk.returncode != 0:
        raise HomesyncError(f"peer engine dir creation failed: {mk.stderr.strip()}")
    proc = subprocess.run(
        ["rsync", "-a", "-e", "ssh " + " ".join(SSH_OPTS),
         *src_args, f"{peer.target}:{dst}"],
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
    if peer.local:
        return local_index(Path(root), recursive=recursive, exclude=exclude,
                           hash_algo=hash_algo, workers=workers)
    if _ssh(peer, f"test -d {shlex.quote(root)}", timeout=30).returncode != 0:
        return []  # absent on the peer -> everything local flows over as additive
    parts = [
        # both engine forms on PYTHONPATH: the .pyz (pushed by a deployed box —
        # listed first, it wins over any stale dir copy from the pre-pyz era)
        # and the package dir (pushed by a source install)
        f"PYTHONPATH=$HOME/{ENGINE_DIR}/fsync.pyz:$HOME/{ENGINE_DIR}",
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
# P7.1 hub-sync: where a profile path lives on the counterpart                #
# --------------------------------------------------------------------------- #

def hub_root(hub: Hub) -> str:
    """Absolute hub root on the hub box (``~`` expands against ITS home)."""
    r = hub.root.rstrip("/") or "~"
    if r == "~":
        return peer_home(hub)
    if r.startswith("~/"):
        return f"{peer_home(hub)}/{r[2:]}"
    return r


def hub_tree(hub: Hub, prof_name: str) -> str:
    """The canonical tree of one profile on the hub (R31)."""
    return f"{hub_root(hub)}/{prof_name}/tree"


def remote_root_for(peer: Peer, prof: Profile, rel: str) -> str:
    """Counterpart root of profile path ``rel``: under the hub tree for a Hub,
    under the peer's live home for a v1 peer. The ONE place this is decided."""
    base = hub_tree(peer, prof.name) if isinstance(peer, Hub) else peer_home(peer)
    return base if rel == "." else f"{base}/{rel}"


def remote_backup_for(peer: Peer, prof: Profile, run_id: str) -> str:
    """Counterpart-side --backup-dir for files this run overwrites there (R4):
    the hub keeps its own backups under its root, a peer under its home."""
    if isinstance(peer, Hub):
        return f"{hub_root(peer)}/backups/{run_id}/{prof.name}"
    return f"{peer_home(peer)}/.fsync/backups/{run_id}/{prof.name}"


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
        # match the probe's host-key policy: accept-new so an off-LAN address
        # (whose key was pinned by the peer_reachable probe moments earlier)
        # isn't refused mid-transfer; BatchMode so it never prompts.
        "-e", DEFAULT_RSYNC_SSH + " -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
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
    local_root = home if rel == "." else home / rel
    remote_root = remote_root_for(peer, prof, rel)
    idx_a = local_index(local_root, recursive=prof.recursive, exclude=prof.exclude, workers=n_workers)
    idx_b = remote_index(peer, remote_root, recursive=prof.recursive, exclude=prof.exclude, workers=n_workers)
    report = compare_file_lists(idx_a, idx_b, match_on="path")
    # allow_renames=False (ISSUE-002): a scheduled run has nobody to act on a
    # rename proposal, and pairs left in plan["renames"] are in no transfer list —
    # they would be silently dropped. Demote them to plain copies instead.
    plan = build_sync_plan(report, conflict=prof.conflict, rename_min_size=prof.rename_min_size,
                           allow_renames=False)
    changed = {pair[0].get("path") for pair in report["name_matches_diff_hash"]}
    # ISSUE-010 fix 3: the receiver's current path set is what resurrection
    # detection compares against its previous one; carried on the plan so the
    # run can save it without re-indexing.
    plan["index_a"] = {it.get("path") for it in idx_a if it.get("path")}
    plan["index_b"] = {it.get("path") for it in idx_b if it.get("path")}
    return local_root, remote_root, plan, changed


# --------------------------------------------------------------------------- #
# ISSUE-010 fix 2: stale-sender guard for SVN working copies in file profiles  #
# --------------------------------------------------------------------------- #

def _local_short_host() -> str:
    return socket.gethostname().split(".")[0]


def svn_guard_for_path(peer: Peer, prof: Profile, rel: str, local_root: Path,
                       remote_root: str, *, head: bool = False) -> dict[str, Any]:
    """Probe the SVN checkouts under one file-profile path on both boxes and
    decide which subtrees this run must not carry (``svnsync.stale_sender_guard``).

    A checkout only this box has needs no peer probe (nothing to compare), so
    the peer round trip happens only when there is something to guard. A peer
    probe that fails is reported (``probe_warning``) and treated as "guard
    inactive" — loudly, never silently."""
    local, heads_l = svs.probe_local(local_root, head=head)
    out: dict[str, Any] = {"checked": 0, "skipped": [], "advisory": [], "excludes": [],
                           "parity": [], "probe_warning": None}
    if not local:
        return out
    remote = svs.probe_remote(lambda cmd, timeout: _ssh(peer, cmd, timeout=timeout),
                              remote_root, head=False)
    if remote is None:
        out["probe_warning"] = (f"{prof.name}/{rel}: svn parity probe failed on {peer.target} — "
                                f"stale-sender guard inactive this run (ISSUE-010)")
        out["parity"] = [{"rel": r, "local_rev": v.get("rev"), "peer_rev": None, "head_rev": None,
                          "state": "unknown", "reason": "peer probe failed"}
                         for r, v in sorted(local.items())]
        return out
    peer_cos, _ = remote
    out.update(svs.stale_sender_guard(local, peer_cos, direction=prof.direction,
                                      local_host=_local_short_host(), peer_host=peer.host,
                                      heads=heads_l))
    return out


# --------------------------------------------------------------------------- #
# ISSUE-010 fix 3: resurrection detection (receiver deleted it since last run) #
# --------------------------------------------------------------------------- #

def _prev_index_path(prof_name: str, key: str, side: str, counterpart: str | None = None) -> Path:
    """``counterpart`` scopes the PEER-side baseline to the tree it describes
    (P7.1: ``hub-<host>``): switching a box from a pairwise peer to the hub
    must start a fresh baseline, or every file the new counterpart lacks reads
    as "resurrected" once (the 2026-11-08 cutover seed reported 160k of them)."""
    tag = f"@{counterpart}" if counterpart else ""
    return state_root() / "prev-index" / prof_name / f"{key}.{side}{tag}.json.gz"


def _load_prev_index(prof_name: str, key: str, side: str,
                     counterpart: str | None = None) -> set[str] | None:
    """The receiver-side path set this box saw on its previous run of this
    profile path, or None when there is no baseline yet."""
    import gzip
    p = _prev_index_path(prof_name, key, side, counterpart)
    try:
        with gzip.open(p, "rt") as f:
            data = json.load(f)
        return set(data.get("paths") or []) if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _save_prev_index(prof_name: str, key: str, side: str, paths: set[str], run_id: str,
                     counterpart: str | None = None) -> None:
    import gzip
    p = _prev_index_path(prof_name, key, side, counterpart)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as f:
            json.dump({"run_id": run_id, "ts": time.time(), "paths": sorted(paths)}, f)
        os.replace(tmp, p)
    except OSError:
        pass


def detect_resurrections(prof_name: str, key: str, plan: dict, a_paths: list[str],
                         b_paths: list[str], changed: set[str],
                         counterpart: str | None = None) -> dict[str, Any]:
    """A path the receiver LACKS now but HAD in the previous run's index was
    deleted on the receiver since — so an additive copy of the sender's still-
    present file re-creates what the receiver removed (ISSUE-010: svn deleted it
    there; ISSUE-007's other half for plain folders and git worktrees). Only
    creates qualify: an overwrite (``changed``) means the receiver still has it.
    Pure given the plan's ``index_a``/``index_b``; absent those (older plan
    shape, tests) detection is off."""
    out: dict[str, Any] = {"on_peer": [], "on_local": [], "count": 0, "truncated": False,
                           "baseline": {"peer": False, "local": False}}
    idx_a, idx_b = plan.get("index_a"), plan.get("index_b")
    if idx_b is not None:
        prev_b = _load_prev_index(prof_name, key, "peer", counterpart)
        if prev_b is not None:
            out["baseline"]["peer"] = True
            out["on_peer"] = [p for p in a_paths if p not in idx_b and p not in changed and p in prev_b]
    if idx_a is not None:
        prev_a = _load_prev_index(prof_name, key, "local")
        if prev_a is not None:
            out["baseline"]["local"] = True
            out["on_local"] = [p for p in b_paths if p not in idx_a and p not in changed and p in prev_a]
    out["count"] = len(out["on_peer"]) + len(out["on_local"])
    return out


# --------------------------------------------------------------------------- #
# sticky skip streaks (ISSUE-006 B/C, shared by git auto-apply and the svn     #
# stale-sender guard): a skip that repeats run after run is a standing         #
# condition, escalated once at AUTO_APPLY_STICKY_RUNS and then periodically    #
# --------------------------------------------------------------------------- #

class StickyStreaks:
    """Streak bookkeeping for one key prefix (``"<profile>/"`` or
    ``"<profile>/svn:"``) in ``<state>/auto-apply-sticky.json``. ``note`` records
    a non-benign skip and returns ``(runs, first_run, log_suffix)``; it also
    appends to ``sticky`` / ``warnings`` at the escalation points (and, with
    ``warn_first``, on the first run of a streak — the svn guard wants the
    operator to hear about a stale checkout right away, not a day later).
    ``finish`` drops streaks that ended and persists. A dry run reads but never
    advances a streak."""

    def __init__(self, prefix: str, *, run_id: str, dry_run: bool) -> None:
        self.prefix = prefix
        self.run_id = run_id
        self.dry_run = dry_run
        self.store = _load_sticky()
        self.stuck_now: set[str] = set()
        self.sticky: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def note(self, name: str, reason: str, *, warn_first: bool = False,
             warn_text: str | None = None, label: str = "auto-apply skipped",
             remedy: str = "receiver work is stuck, reconcile by hand (ISSUE-006)") -> tuple[int, str, str]:
        key = f"{self.prefix}{name}"
        self.stuck_now.add(key)
        prev = self.store.get(key) or {}
        same = prev.get("reason_key") == _sticky_key(reason)
        runs = int(prev.get("runs", 0)) if same else 0
        first = prev.get("first_run", self.run_id) if same else self.run_id
        if self.dry_run:
            return runs, first, (f" [{runs} in a row since {first}]" if runs > 1 else "")
        runs += 1
        self.store[key] = {"reason": reason, "reason_key": _sticky_key(reason),
                           "runs": runs, "first_run": first, "last_run": self.run_id}
        if runs >= AUTO_APPLY_STICKY_RUNS:
            self.sticky.append({"repo": name, "reason": reason, "runs": runs, "first_run": first})
        if (warn_first and runs == 1) or runs == AUTO_APPLY_STICKY_RUNS or (
                runs > AUTO_APPLY_STICKY_RUNS
                and (runs - AUTO_APPLY_STICKY_RUNS) % AUTO_APPLY_REMIND_EVERY == 0):
            self.warnings.append(warn_text if (warn_text and runs == 1) else
                                 f"{key}: {label} {runs} runs in a row since {first} "
                                 f"({reason}) — {remedy}")
        return runs, first, (f" [{runs} in a row since {first}]" if runs > 1 else "")

    def finish(self) -> None:
        if self.dry_run:
            return
        for key in [k for k in self.store if k.startswith(self.prefix) and k not in self.stuck_now]:
            del self.store[key]
        try:
            _atomic_write_json(_sticky_path(), self.store)
        except OSError:
            pass


def build_run_preview(
    peer: Peer,
    selected: list[Profile],
    workers: int | None = None,
    sample_n: int = 8,
    progress_path: str | Path | None = None,
    control=None,
) -> dict[str, Any]:
    """Plan every selected profile without transferring: 'what is coming'.

    The executing run re-plans from fresh indexes, so these numbers are a
    preview — live trees can drift between confirm and execute; the run
    report is authoritative.

    With ``progress_path``, an atomically-replaced snapshot of per-path
    planning state (queued / indexing / done+counts) is maintained so a UI
    can render the plan filling in while slower paths still hash."""
    prog: dict[str, Any] | None = None

    def prog_write() -> None:
        if prog is not None:
            _atomic_write_json(Path(progress_path), prog)

    if progress_path:
        prog = {"status": "planning",
                "profiles": {p.name: {"paths": {rel: {"phase": "queued"} for rel in p.paths}}
                             for p in selected}}
        prog_write()

    out: dict[str, Any] = {"profiles": {}}
    for prof in selected:
        if prof.kind == "git":
            # Git profiles don't rsync; the preview lists their repos instead.
            out["profiles"][prof.name] = {"kind": "git",
                                          "repos": [r["rel"] for r in _discover_git_repos(prof)]}
            continue
        if prof.kind == "svn":
            out["profiles"][prof.name] = {"kind": "svn",
                                          "checkouts": [c["rel"] for c in _discover_svn_checkouts(prof)]}
            continue
        pp: dict[str, Any] = {"conflict": prof.conflict, "direction": prof.direction, "paths": {},
                              "svn": []}
        for rel in prof.paths:
            if prog is not None:
                prog["profiles"][prof.name]["paths"][rel] = {"phase": "svn-parity"}
                prog_write()
            # ISSUE-010 fix 2/6: the preview applies the same stale-sender guard
            # as the run, and carries per-checkout parity (sender r / receiver r
            # / server HEAD r) so a stale checkout is visible BEFORE pressing run.
            home = Path.home()
            guard = svn_guard_for_path(peer, prof, rel,
                                       home if rel == "." else home / rel,
                                       remote_root_for(peer, prof, rel), head=True)
            for row in guard["parity"]:
                pp["svn"].append({"path": rel, **row})
            if guard.get("probe_warning"):
                pp["svn"].append({"path": rel, "rel": "*", "state": "unknown",
                                  "reason": guard["probe_warning"], "local_rev": None,
                                  "peer_rev": None, "head_rev": None})
            plan_prof = (replace(prof, exclude=list(prof.exclude) + guard["excludes"])
                         if guard["excludes"] else prof)
            if prog is not None:
                prog["profiles"][prof.name]["paths"][rel] = {"phase": "indexing"}
                prog_write()
            if isinstance(peer, Hub) and prof.mode == "full" and control is not None:
                key = "home" if rel == "." else rel.replace("/", "_")
                _, _, L, live, _t, _b, head, fplan, _rm, _cv = _plan_full_path(
                    control, peer, plan_prof, rel, key, workers or prof.workers)
                pp["paths"][rel] = {
                    "mode": "full", "seq": head,
                    "a_to_b": len(fplan.push), "b_to_a": len(fplan.pull),
                    "bytes_a_to_b": sum((L.get(x) or {}).get("size") or 0 for x in fplan.push),
                    "bytes_b_to_a": sum((live.get(x) or [0])[0] or 0 for x in fplan.pull),
                    "overwrites_a_to_b": len(fplan.overwrite_push),
                    "overwrites_b_to_a": len(fplan.overwrite_pull),
                    "del_hub": len(fplan.del_hub), "del_local": len(fplan.del_local),
                    "conflicts": len(fplan.conflicts), "identical": fplan.noop,
                    "renames_demoted": 0, "svn_skipped": len(guard["skipped"]),
                    "svn_checked": guard["checked"],
                    "stranded": len(fplan.stranded_push) + len(fplan.stranded_pull)
                    + len(fplan.stranded_del_hub) + len(fplan.stranded_del_local),
                    "mass_delete_held": len(fplan.mass_delete_held),
                    "samples": {"a_to_b": fplan.push[:sample_n], "b_to_a": fplan.pull[:sample_n],
                                "del_hub": fplan.del_hub[:sample_n], "del_local": fplan.del_local[:sample_n]},
                }
                if prog is not None:
                    prog["profiles"][prof.name]["paths"][rel] = {"phase": "done", **pp["paths"][rel]}
                    prog_write()
                continue
            _, _, plan, changed = _plan_path(peer, plan_prof, rel, workers or prof.workers)
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
                "svn_skipped": len(guard["skipped"]),
                "svn_checked": guard["checked"],
                "samples": {
                    "a_to_b": [it["path"] for it in a_items[:sample_n]],
                    "b_to_a": [it["path"] for it in b_items[:sample_n]],
                },
            }
            if prog is not None:
                prog["profiles"][prof.name]["paths"][rel] = {"phase": "done", **pp["paths"][rel]}
                prog_write()
        out["profiles"][prof.name] = pp
    if prog is not None:
        prog["status"] = "done"
        prog_write()
    return out


MAX_GHOST_SCAN = 2000  # cap probe args to bound command length + latency


def _scan_untracked_introductions(root: str, rel_paths: list[str], *,
                                  peer: Peer | None) -> tuple[list[str], bool]:
    """Fix A (ISSUE-001): after a file leg, report which transferred paths landed
    UNTRACKED in a git worktree on the receiving side — the cross-branch ghost
    signature. Runs the probe on the peer (ssh) or locally. Best-effort: any
    failure returns no ghosts rather than breaking the run. Returns
    (ghost_rel_paths, truncated)."""
    paths = [p for p in rel_paths if p]
    if not paths:
        return [], False
    truncated = len(paths) > MAX_GHOST_SCAN
    paths = paths[:MAX_GHOST_SCAN]
    try:
        if peer is not None:
            cmd = ("python3 -c " + shlex.quote(grs.GHOST_PROBE) + " "
                   + shlex.quote(root) + " "
                   + " ".join(shlex.quote(p) for p in paths))
            proc = _ssh(peer, cmd, timeout=180)
        else:
            proc = subprocess.run(["python3", "-c", grs.GHOST_PROBE, root, *paths],
                                  capture_output=True, text=True, timeout=180)
    except (subprocess.SubprocessError, OSError, HomesyncError):
        return [], truncated
    ghosts = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return ghosts, truncated


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
    control=None,
    lease_id: str | None = None,
    allow_mass_delete: bool = False,
) -> dict[str, Any]:
    """Index both sides, plan, and execute every path of one profile."""
    n_workers = workers or prof.workers
    full_mode = isinstance(peer, Hub) and prof.mode == "full" and control is not None
    local_backup = str(Path("~/.fsync/backups").expanduser() / run_id / prof.name)
    remote_backup = remote_backup_for(peer, prof, run_id)

    result: dict[str, Any] = {"conflict": prof.conflict, "paths": {}, "status": "ok",
                              "warnings": []}
    streaks = StickyStreaks(f"{prof.name}/svn:", run_id=run_id, dry_run=dry_run)
    for rel in prof.paths:
        started = time.time()
        key = "home" if rel == "." else rel.replace("/", "_")

        # ISSUE-010 fix 2: before indexing, find SVN checkouts whose sender is
        # behind the receiver (or on another URL) and exclude those subtrees —
        # a stale sender's clean, versioned files would re-create everything
        # svn deleted on the receiver since. Sticky + attention + notified, like
        # kind:git's "receiver has local changes".
        home = Path.home()
        g_local = home if rel == "." else home / rel
        g_remote = remote_root_for(peer, prof, rel)
        if isinstance(peer, Hub) and not dry_run:
            # The hub tree is ours to create (R31): a fresh hub or a profile
            # added later has no directory yet, and rsync --files-from makes
            # only the last path component.
            mk = _ssh(peer, f"mkdir -p {shlex.quote(g_remote)}", timeout=30)
            if mk.returncode != 0:
                raise HomesyncError(f"cannot create hub tree {g_remote}: {mk.stderr.strip()}")
        if progress:
            progress.path_phase(prof.name, rel, "svn-parity")
        guard = svn_guard_for_path(peer, prof, rel, g_local, g_remote)
        if guard.get("probe_warning"):
            result["warnings"].append(guard["probe_warning"])
            log(f"  ! {guard['probe_warning']}")
        for adv in guard["advisory"]:
            log(f"  note: {prof.name}/{rel}: {adv['reason']}")
        for sk in guard["skipped"]:
            runs, first, suffix = streaks.note(
                f"{rel}/{sk['rel']}", sk["reason"], warn_first=True,
                warn_text=f"{prof.name}/{rel}: {sk['reason']} (ISSUE-010)",
                label="svn checkout skipped", remedy="svn update the stale side (ISSUE-010)")
            log(f"  ! {prof.name}/{rel}: SKIPPED {sk['reason']}{suffix}")
        plan_prof = (replace(prof, exclude=list(prof.exclude) + guard["excludes"])
                     if guard["excludes"] else prof)

        if progress:
            progress.path_phase(prof.name, rel, "indexing")

        if full_mode:
            # P7.2: three-way vs base with hub tombstones + journal (docs/hub-sync.md R32–R35)
            pr = _run_path_full(control, peer, plan_prof, rel, key, n_workers, run_dir=run_dir,
                                run_id=run_id, dry_run=dry_run, log=log, progress=progress,
                                local_backup=local_backup, remote_backup=remote_backup,
                                lease_id=lease_id, allow_mass_delete=allow_mass_delete)
            pr["svn_guard"] = {"checked": guard["checked"], "skipped": guard["skipped"],
                               "advisory": guard["advisory"], "parity": guard["parity"],
                               "probe_warning": guard.get("probe_warning")}
            pr["seconds"] = round(time.time() - started, 2)
            result["paths"][rel] = pr
            if progress:
                progress.path_done(prof.name, rel, pr)
            if pr["attention"] or guard["skipped"]:
                result["status"] = "attention"
            result["warnings"].extend(pr["warnings"])
            continue

        local_root, remote_root, plan, changed_paths = _plan_path(peer, plan_prof, rel, n_workers)

        # Overwrite = a routed same-path/different-content file; exactly those
        # produce a backup on the receiver. Additive copies overwrite nothing.
        a_paths = [it["path"] for it in plan["a_to_b"] if it.get("path")]
        b_paths = [it["path"] for it in plan["b_to_a"] if it.get("path")]

        # Union-merge interception: files matching merge_jsonl that differ on
        # both sides are resolved by line-union (never clobbered, never held).
        # The merged result is written locally (old copy backed up) and pushed
        # when direction allows; under pull only the local side gains the union.
        # A dry run is a preview and writes nothing: it reports the merges a
        # real run would perform and leaves both sides byte-for-byte untouched.
        merged_files: list[str] = []
        would_merge: list[str] = []
        if prof.merge_jsonl:
            merge_set = {p for p in changed_paths
                         if p and matches_exclude(p, prof.merge_jsonl)}
            if merge_set:
                a_paths = [p for p in a_paths if p not in merge_set]
                b_paths = [p for p in b_paths if p not in merge_set]
                kept_pairs = {pr[0].get("path"): pr for pr in plan["conflicts"]}
                plan["conflicts"] = [pr for pr in plan["conflicts"]
                                     if pr[0].get("path") not in merge_set]
            if merge_set and dry_run:
                would_merge = sorted(merge_set)
                if prof.direction in ("both", "push"):
                    a_paths.extend(would_merge)
            elif merge_set:
                if progress:
                    progress.path_phase(prof.name, rel, "merging")
                for relp in sorted(merge_set):
                    try:
                        proc = _ssh(peer, f"cat {shlex.quote(remote_root + '/' + relp)}")
                        if proc.returncode != 0:
                            raise HomesyncError(proc.stderr.strip().splitlines()[-1]
                                                if proc.stderr.strip() else f"cat rc={proc.returncode}")
                        local_file = local_root / relp
                        merged = union_merge_jsonl(local_file.read_text(), proc.stdout)
                        bak = Path(local_backup) / relp
                        bak.parent.mkdir(parents=True, exist_ok=True)
                        bak.write_text(local_file.read_text())
                        tmp = local_file.with_suffix(local_file.suffix + ".fsync-merge")
                        tmp.write_text(merged)
                        os.replace(tmp, local_file)
                        merged_files.append(relp)
                        if prof.direction in ("both", "push"):
                            a_paths.append(relp)
                    except (OSError, HomesyncError) as e:
                        # merge failed -> keep it a held conflict, never guess
                        if relp in kept_pairs:
                            plan["conflicts"].append(kept_pairs[relp])
                        log(f"  {prof.name}/{rel}: merge of {relp} failed ({e}) — held for review")

        # Direction filters ROUTING, it never overrides newest-wins: in push
        # mode a file the peer has newer is skipped (reported), not clobbered
        # with our older copy. One-way here is still additive — no deletes.
        #
        # ISSUE-003: keep the PATHS, not just a count. Every path dropped here is
        # in the plan precisely because it diverges — byte-identical files never
        # leave plan["noop"] — so a one-way profile withholding them is real,
        # unreported divergence, not a no-op. "I chose not to copy this" and
        # "there was nothing to copy" must not render identically.
        stranded: list[str] = []
        if prof.direction == "push":
            stranded, b_paths = b_paths, []
        elif prof.direction == "pull":
            stranded, a_paths = a_paths, []
        skipped_by_direction = len(stranded)
        # A withheld path is one of two very different things: a file the
        # withheld side ALONE has (work done there, or a file deleted here that
        # additive sync never removes there — ISSUE-007), or a file both sides
        # have whose copy there is newer. Only the latter "differs from the
        # peer"; name them separately so the remedy is obvious.
        stranded_newer = sum(1 for p in stranded if p in changed_paths)
        stranded_only = skipped_by_direction - stranded_newer
        stranded_side = "peer" if prof.direction == "push" else "local"
        stranded_where = "the peer" if prof.direction == "push" else "this box"

        over_ab = sum(1 for p in a_paths if p in changed_paths)
        over_ba = sum(1 for p in b_paths if p in changed_paths)

        # ISSUE-010 fix 3: creates on a receiver that HAD the path last run are
        # resurrections of something it deleted since. Detection, not a block:
        # the undo is the listed paths (backup_root holds nothing for creates).
        # the peer-side baseline belongs to the counterpart tree it describes
        counterpart = f"hub-{peer.host}" if isinstance(peer, Hub) else None
        resurrected = detect_resurrections(prof.name, key, plan, a_paths, b_paths, changed_paths,
                                           counterpart=counterpart)
        if resurrected["count"]:
            lst = run_dir / prof.name / f"{key}.resurrected.lst"
            lst.parent.mkdir(parents=True, exist_ok=True)
            lst.write_text("".join(f"peer\t{p}\n" for p in resurrected["on_peer"])
                           + "".join(f"local\t{p}\n" for p in resurrected["on_local"]))
            resurrected["list"] = str(lst)
        if plan.get("index_a") is not None and plan.get("index_b") is not None:
            _save_prev_index(prof.name, key, "local", plan["index_a"], run_id)
            _save_prev_index(prof.name, key, "peer", plan["index_b"], run_id,
                             counterpart=counterpart)

        if progress:
            progress.path_phase(prof.name, rel, "a_to_b", planned=len(a_paths))
        leg_ab = run_rsync_leg(
            a_paths, str(local_root), peer.spec(remote_root), remote_backup,
            run_dir / prof.name / f"{key}.a_to_b.lst", dry_run=dry_run, overwrites=over_ab,
            on_progress=(lambda b, p, fd, ft: progress.leg_progress(prof.name, rel, "a_to_b", b, p, fd, ft))
            if progress else None,
        )
        if progress:
            progress.path_phase(prof.name, rel, "b_to_a", planned=len(b_paths))
        leg_ba = run_rsync_leg(
            b_paths, peer.spec(remote_root), str(local_root), local_backup,
            run_dir / prof.name / f"{key}.b_to_a.lst", dry_run=dry_run, overwrites=over_ba,
            on_progress=(lambda b, p, fd, ft: progress.leg_progress(prof.name, rel, "b_to_a", b, p, fd, ft))
            if progress else None,
        )

        # Fix A (ISSUE-001): flag transferred files that landed untracked on the
        # receiver (cross-branch ghosts). a_paths went to the peer; b_paths came
        # here. Detection only — never fails the run.
        ghosts_peer = ghosts_local = []
        ghosts_truncated = False
        if not dry_run:
            if a_paths:
                ghosts_peer, tp = _scan_untracked_introductions(remote_root, a_paths, peer=peer)
                ghosts_truncated = ghosts_truncated or tp
            if b_paths:
                ghosts_local, tl = _scan_untracked_introductions(str(local_root), b_paths, peer=None)
                ghosts_truncated = ghosts_truncated or tl
        ghost_total = len(ghosts_peer) + len(ghosts_local)

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
            "direction": prof.direction,
            "merged": merged_files,
            "would_merge": would_merge,
            "skipped_by_direction": skipped_by_direction,
            "stranded": stranded[:STRANDED_SAMPLE],
            "stranded_truncated": skipped_by_direction > STRANDED_SAMPLE,
            "stranded_side": stranded_side,
            "stranded_only_on_side": stranded_only,
            "stranded_newer_on_side": stranded_newer,
            "conflicts": len(conflicts),
            "renames_pending": len(plan["renames"]),
            "renames_demoted": len(plan["renames_suppressed"]),
            "identical": plan["noop"],
            "untracked_introductions": {
                "on_peer": ghosts_peer, "on_local": ghosts_local,
                "count": ghost_total, "truncated": ghosts_truncated,
            },
            "resurrected": {
                "on_peer": resurrected["on_peer"][:RESURRECTED_SAMPLE],
                "on_local": resurrected["on_local"][:RESURRECTED_SAMPLE],
                "count": resurrected["count"],
                "truncated": resurrected["count"] > RESURRECTED_SAMPLE,
                "list": resurrected.get("list"),
                "baseline": resurrected["baseline"],
            },
            "svn_guard": {
                "checked": guard["checked"],
                "skipped": guard["skipped"],
                "advisory": guard["advisory"],
                "parity": guard["parity"],
                "probe_warning": guard.get("probe_warning"),
            },
            "seconds": round(time.time() - started, 2),
        }
        if progress:
            progress.path_done(prof.name, rel, result["paths"][rel])
        if ghost_total:
            sample = (ghosts_peer + ghosts_local)[:5]
            log(f"  ! {prof.name}/{rel}: {ghost_total} transferred file(s) UNTRACKED on the "
                f"receiver — possible cross-branch ghost (ISSUE-001): "
                + ", ".join(sample) + (" ..." if ghost_total > 5 else ""))
        # ISSUE-003 / ISSUE-002: withheld or unactioned divergence must never
        # render green. "ok" has to mean "the two sides agree", not "the run
        # finished without raising".
        if stranded or plan["renames"] or guard["skipped"] or resurrected["count"]:
            result["status"] = "attention"
        if resurrected["count"]:
            sample = (resurrected["on_peer"] + resurrected["on_local"])[:5]
            where = ("the peer" if resurrected["on_peer"] else "this box")
            msg = (f"{prof.name}/{rel}: {resurrected['count']} file(s) RESURRECTED on {where} — "
                   f"the receiver had deleted them since the last run; this copy re-created them "
                   f"(ISSUE-010): " + ", ".join(sample) + (" ..." if resurrected["count"] > 5 else "")
                   + f" | undo: remove the paths listed in {resurrected.get('list')}")
            log(f"  ! {msg}")
            result["warnings"].append(msg[:300])
        if stranded:
            shown = stranded[:5]
            log(f"  ! {prof.name}/{rel}: {skipped_by_direction} file(s) STRANDED by "
                f"direction:{prof.direction} — withheld, not synced: {stranded_only} exist "
                f"only on {stranded_where}, {stranded_newer} are newer on {stranded_where}: "
                + ", ".join(shown)
                + (" ..." if skipped_by_direction > 5 else "")
                + f" | reconcile with: fsync sync folder {rel}")
        if plan["renames"]:
            log(f"  ! {prof.name}/{rel}: {len(plan['renames'])} rename pair(s) UNACTIONED "
                f"(ISSUE-002) — neither copied nor renamed; this caller should pass "
                f"allow_renames=False")
        log(
            f"  {prof.name}/{rel}: A->B {len(a_paths)}"
            + (f" ({over_ab} overwrite->backup)" if over_ab else "")
            + f", B->A {len(b_paths)}"
            + (f" ({over_ba} overwrite->backup)" if over_ba else "")
            + f", conflicts {len(conflicts)}, identical {plan['noop']}"
            + (f", merged {len(merged_files)}" if merged_files else "")
            + (f", would merge {len(would_merge)}" if would_merge else "")
            + (f", {prof.direction}-only ({skipped_by_direction} STRANDED)"
               if prof.direction != "both" and skipped_by_direction else
               (f", {prof.direction}-only" if prof.direction != "both" else ""))
            + (f", svn {guard['checked']} checked/{len(guard['skipped'])} skipped"
               if guard["checked"] or guard["skipped"] else "")
            + f" [{result['paths'][rel]['seconds']}s]"
        )
    streaks.finish()
    result["warnings"].extend(streaks.warnings)
    result["sticky"] = streaks.sticky
    return result


# --------------------------------------------------------------------------- #
# CLI entry                                                                   #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# P5 git-repo-sync: kind=git profiles carry full git working state via bundles #
# (docs/git-repo-sync.md). Snapshot is producer-side and timer-safe; apply is  #
# receiver-side, on-demand, and always backs the receiver up first (R15).      #
# --------------------------------------------------------------------------- #

def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _discover_git_repos(prof: Profile) -> list[dict]:
    """Repos under a git profile's paths, as ``{name, rel, path}`` (rel = path
    relative to $HOME, shared by both boxes)."""
    home = _home()
    out: list[dict] = []
    seen: set[str] = set()
    for p in prof.paths:
        for name, path in grs.discover_repos(home / p, prof.exclude):
            rel = path.relative_to(home).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            out.append({"name": name, "rel": rel, "path": path})
    return out


def git_profile_snapshots(prof: Profile) -> bool:
    """A git profile produces bundles only on the pushing side (push/both). A
    pull-only box is a receiver and must not create competing snapshots (they'd
    race the producer's under newest-wins). Manual `fsync git snapshot` ignores
    this — it's explicit — the gate is only for the automated `sync run`."""
    return prof.kind == "git" and prof.direction in ("push", "both")


def _note_kind_error(report: dict, prof: Profile, e: Exception, log) -> int:
    """Record a kind profile's snapshot failure in the run report (status
    error, message in ``errors``) without aborting the profile; returns the
    run's rc (1)."""
    report["profiles"][prof.name] = {"kind": prof.kind, "status": "error",
                                     "snapshot_error": str(e)}
    report["errors"].append(f"{prof.name}: {e}")
    log(f"  {prof.name}: ERROR {e}")
    return 1


def auto_apply_git_repos(prof: Profile, *, via_hub: bool = False) -> bool:
    """Receiver-side auto-apply runs for a kind:git profile set to apply:auto that
    is pull-only (the durable ISSUE-001 fix C) — or, under a hub (P7.3), also
    ``direction: both``: there the registry hands a box only the newest snapshot
    of ANOTHER host, so a box can snapshot its own repos and apply the others'
    in the same run without ever applying itself. Without a hub the carrier dir
    holds this box's own bundles too, so the pairwise rule stays pull-only."""
    if prof.kind != "git" or prof.apply != "auto":
        return False
    if via_hub:
        return prof.direction in ("pull", "both")
    return not git_profile_snapshots(prof)


def git_repo_excludes_for_file_profiles(profiles: dict[str, Profile]) -> dict[str, list[str]]:
    """Fix C: repos owned by an apply:auto kind:git profile must not be
    double-carried by file profiles (that additive, branch-unaware copy is the
    ISSUE-001 ghost transport). Returns {file_profile: [subtree exclude patterns]}
    — each pattern is the repo path RELATIVE TO that file profile's indexed root,
    so only the git-owned subtree is skipped; the fsync repo, SVN checkouts, and
    plain dirs are left alone. Empty (a no-op with zero fs cost) when no git
    profile is apply:auto — so the default behaviour is unchanged."""
    auto_rels: list[str] = []
    for p in profiles.values():
        if p.kind == "git" and p.apply == "auto":
            auto_rels.extend(r["rel"] for r in _discover_git_repos(p))
        elif p.kind == "svn" and p.apply == "auto":
            auto_rels.extend(r["rel"] for r in _discover_svn_checkouts(p))
    out: dict[str, list[str]] = {}
    if not auto_rels:
        return out
    for name, p in profiles.items():
        if p.kind != "files":
            continue
        pats: list[str] = []
        for base in p.paths:
            for rel in auto_rels:
                if rel == base:
                    pats.append("*")
                elif rel.startswith(base + "/"):
                    pats.append(rel[len(base) + 1:] + "/*")
        if pats:
            out[name] = sorted(set(pats))
    return out


def _auto_apply_decision(target: Path, meta: dict[str, Any]) -> tuple[bool, str]:
    """Guard for receiver auto-apply — apply only when it cannot lose work: a
    fresh (absent) repo, or one that is not busy, clean, and already on the
    producer's branch. Any other state skips (the repo simply stays as-is, since
    a prior file sync already left content there) and is reported with a reason."""
    if not grs.is_git_repo(target):
        return True, "new"
    busy = grs.repo_busy(target)
    if busy:
        return False, f"receiver {busy}"
    if grs.is_dirty(target):
        return False, "receiver has local changes"
    div = grs.check_branch_divergence(target, meta)
    if div:
        return False, (f"branch divergence (receiver {div['receiver_branch'] or 'DETACHED'}, "
                       f"producer {div['producer_branch'] or 'DETACHED'})")
    # ISSUE-006 sub-finding: a stale bundle whose head the receiver already
    # contains would move the branch backwards — the receiver is ahead, not
    # behind. Skip; the cure is a producer re-snapshot, never a rollback.
    ahead = grs.check_receiver_ahead(target, meta)
    if ahead:
        return False, (f"receiver is ahead of incoming ({ahead['incoming_head'][:9]} is an "
                       f"ancestor of {ahead['receiver_head'][:9]}) — stale bundle; "
                       f"re-snapshot the producer")
    # Already at the producer's exact state — skip so we don't re-apply (and R15
    # back up) all repos every run when nothing changed. A dirty producer
    # (meta.dirty) means the worktree differs from HEAD, so still apply.
    hp = grs._git(target, "rev-parse", "HEAD", check=False)
    recv_head = hp.stdout.strip() if hp.returncode == 0 else None
    if recv_head and recv_head == meta.get("head") and not meta.get("dirty"):
        return False, "up to date"
    return True, "clean"


AUTO_APPLY_STICKY_RUNS = 24        # ISSUE-006 fix B: consecutive skips before escalating
AUTO_APPLY_REMIND_EVERY = 24 * 7   # ...then repeat the reminder this often
_STICKY_BENIGN = {"up to date", "no bundle"}   # skip reasons that are not a stuck receiver


def _sticky_path() -> Path:
    return state_root() / "auto-apply-sticky.json"


def _load_sticky() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_sticky_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _sticky_key(reason: str) -> str:
    """The reason modulo commit ids, so a producer that keeps committing does
    not reset a 'receiver has commits the bundle lacks' streak."""
    return re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", reason)


def auto_apply_git_profile(prof: Profile, bundle_dir: str | Path, *,
                           run_id: str, dry_run: bool, log,
                           hub: "Hub | None" = None, control=None) -> dict[str, Any]:
    """Receiver side (apply:auto): apply each staged bundle whose repo belongs to
    this profile, guarded by ``_auto_apply_decision``. Every real apply backs the
    receiver up first (R15). Bundles are matched to the profile by their meta
    ``rel`` falling under the profile's paths.

    ISSUE-006 fixes B + C: a skip that repeats run after run is a receiver whose
    work is stuck, not a benign state. Streaks persist in
    ``<state>/auto-apply-sticky.json``; at ``AUTO_APPLY_STICKY_RUNS`` (and every
    ``AUTO_APPLY_REMIND_EVERY`` after) the repo is named in ``warnings`` — which
    the run turns into a desktop notification — and the profile carries a
    ``status``: ``ok`` / ``attention`` (anything skipped for a non-benign
    reason) / ``error``. A dry run reads the streaks but never advances them."""
    bundle_dir = Path(bundle_dir).expanduser()
    home = _home()
    via_hub = hub is not None and control is not None
    backup_dir = Path(DEFAULT_GIT_BACKUP_ROOT).expanduser() / f"{run_id}-autoapply"
    out: dict[str, Any] = {"kind": "git", "apply": "auto", "status": "ok",
                           "applied": [], "skipped": [], "errors": [],
                           "sticky": [], "warnings": [], "store": "hub" if via_hub else "bundles"}
    # (meta, source, namespace): a bundle file, or the hub repo + producer namespace
    items: list[tuple[dict, str, str | None]] = []
    if via_hub:
        from .hub import hub_git_url
        try:
            metas = _hub_kind_metas(control, "git", prof, self_host=_local_short_host())
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"hub registry: {e}")
            out["status"] = "error"
            return out
        for meta in metas:
            items.append((meta, hub_git_url(hub, meta["rel"]), meta.get("namespace")
                          or grs.hub_namespace(meta["host"])))
    else:
        if not bundle_dir.is_dir():
            return out
        for meta_path in sorted(bundle_dir.glob("*.meta.json")):
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, ValueError):
                continue
            items.append((meta, str(bundle_dir / f"{meta.get('name') or Path(meta.get('rel') or '').name}.bundle"), None))
    streaks = StickyStreaks(f"{prof.name}/", run_id=run_id, dry_run=dry_run)

    def note_skip(name: str, reason: str) -> str:
        """Record a non-benign skip's streak; return a suffix for the log line."""
        return streaks.note(name, reason)[2]

    for meta, source, namespace in items:
        rel = meta.get("rel")
        if not rel or not any(rel == b or rel.startswith(b + "/") for b in prof.paths):
            continue
        name = meta.get("name") or Path(rel).name
        target = home / rel
        if namespace is None and not Path(source).exists():
            out["skipped"].append({"repo": name, "reason": "no bundle"})
            continue
        ok, reason = _auto_apply_decision(target, meta)
        head = (meta.get("head") or "")[:9]
        if not ok:
            out["skipped"].append({"repo": name, "reason": reason})
            suffix = "" if reason in _STICKY_BENIGN else note_skip(name, reason)
            log(f"  {prof.name}/{name}: auto-apply skipped ({reason}){suffix}")
            continue
        if dry_run:
            out["applied"].append({"repo": name, "dry_run": True})
            log(f"  {prof.name}/{name}: would auto-apply {meta.get('branch') or 'DETACHED'}@{head}")
            continue
        try:
            res = grs.apply_repo(target, source, meta, backup_dir=backup_dir, namespace=namespace)
            out["applied"].append({"repo": name, "head": meta.get("head"),
                                   "backup": res.get("backup")})
            log(f"  {prof.name}/{name}: auto-applied {meta.get('branch') or 'DETACHED'}@{head}"
                + (f" (backup {res['backup']})" if res.get("backup") else ""))
        except grs.ReceiverAheadError as e:
            # Post-fetch divergence guard (ISSUE-006 sub-finding). Same family as
            # the pre-fetch "receiver is ahead" skip: a standing condition only a
            # human resolves — so a sticky skip (attention + streak escalation),
            # not an error that fails, and notifies, every hour until then.
            reason = (f"receiver has commits the incoming bundle lacks (receiver HEAD "
                      f"{e.receiver_head[:9]} is not an ancestor of incoming "
                      f"{e.incoming_head[:9]}) — bring the receiver's commits to the "
                      f"producer, or `fsync git apply --force` to roll back")
            out["skipped"].append({"repo": name, "reason": reason})
            log(f"  {prof.name}/{name}: auto-apply skipped ({reason}){note_skip(name, reason)}")
        except grs.GitSyncError as e:
            out["errors"].append(f"{name}: {e}")
            log(f"  {prof.name}/{name}: auto-apply ERROR {e}")

    if out["errors"]:
        out["status"] = "error"
    elif any(s["reason"] not in _STICKY_BENIGN for s in out["skipped"]):
        out["status"] = "attention"
    # A repo applied, up to date, or gone this run ends its streak.
    streaks.finish()
    out["sticky"].extend(streaks.sticky)
    out["warnings"].extend(streaks.warnings)
    return out


# --------------------------------------------------------------------------- #
# ISSUE-010 fix 5: kind=svn profiles (fsync/svnsync.py) — same shape as git:   #
# producer snapshots, carrier file profile moves the snapshot dir, receiver    #
# applies on demand (`fsync svn apply`) or auto (guarded) when apply: auto.    #
# --------------------------------------------------------------------------- #

def _discover_svn_checkouts(prof: Profile) -> list[dict]:
    home = _home()
    out: list[dict] = []
    seen: set[str] = set()
    for p in prof.paths:
        for name, path in svs.discover_checkouts(home / p, prof.exclude):
            rel = path.relative_to(home).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            out.append({"name": name, "rel": rel, "path": path})
    return out


def svn_profile_snapshots(prof: Profile) -> bool:
    return prof.kind == "svn" and prof.direction in ("push", "both")


def auto_apply_svn_checkouts(prof: Profile, *, via_hub: bool = False) -> bool:
    """Same rule as ``auto_apply_git_repos``: pull-only, or ``both`` under a hub."""
    if prof.kind != "svn" or prof.apply != "auto":
        return False
    if via_hub:
        return prof.direction in ("pull", "both")
    return not svn_profile_snapshots(prof)


def _svn_snapshot_files(snap_dir: Path, meta: dict[str, Any]) -> list[Path]:
    files = [snap_dir / f"{meta['name']}.svn.meta.json"]
    for key in ("diff", "unversioned_tar"):
        if meta.get(key):
            files.append(snap_dir / meta[key])
    return [f for f in files if f.exists()]


def _push_svn_snapshot_to_hub(hub: "Hub", prof: Profile, rel: str, host: str, snap_dir: Path,
                              meta: dict[str, Any]) -> None:
    """P7.3: the three snapshot files go to ``<hub>/svn/<rel>/<host>/`` (rsync,
    --delete so a vanished tar does not linger); the meta is registered after."""
    from .hub import hub_svn_path
    dest = hub_svn_path(hub, rel, host)
    mk = _ssh(hub, f"mkdir -p {shlex.quote(dest)}", timeout=30)
    if mk.returncode != 0:
        raise HomesyncError(f"cannot create hub svn store {dest}: {mk.stderr.strip()}")
    files = _svn_snapshot_files(snap_dir, meta)
    proc = subprocess.run(
        ["rsync", "-a", "--delete", "-e", DEFAULT_RSYNC_SSH + " -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
         *[str(f) for f in files], hub.spec(dest).rstrip("/") + "/"],
        capture_output=True, text=True, timeout=900)
    if proc.returncode not in TOLERATED_RSYNC_EXITS:
        raise HomesyncError(f"svn snapshot upload failed (rc={proc.returncode}): "
                            + "\n".join(proc.stderr.strip().splitlines()[-3:]))


def _pull_svn_snapshot_from_hub(hub: "Hub", meta: dict[str, Any], staging: Path) -> Path:
    """Receiver side: mirror ``<hub>/svn/<rel>/<host>/`` into a local staging dir
    the existing ``svnsync.apply_wc`` reads from."""
    from .hub import hub_svn_path
    src = hub_svn_path(hub, meta["rel"], meta["host"])
    staging.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["rsync", "-a", "--delete", "-e", DEFAULT_RSYNC_SSH + " -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
         hub.spec(src).rstrip("/") + "/", str(staging).rstrip("/") + "/"],
        capture_output=True, text=True, timeout=900)
    if proc.returncode not in TOLERATED_RSYNC_EXITS:
        raise svs.SvnSyncError(f"svn snapshot download failed (rc={proc.returncode}): "
                               + "\n".join(proc.stderr.strip().splitlines()[-3:]))
    return staging


def snapshot_svn_profile(prof: Profile, snap_dir: str | Path, log, *,
                         hub: "Hub | None" = None, control=None) -> dict[str, Any]:
    """Producer side: capture every working copy in the profile. Read-only on
    the checkouts; per-checkout isolation like the git snapshot (one locked or
    conflicted WC never stops the rest). With a hub (P7.3) each snapshot's files
    are uploaded to the hub's svn store and its meta registered there."""
    snap_dir = Path(snap_dir).expanduser()
    snap_dir.mkdir(parents=True, exist_ok=True)
    via_hub = hub is not None and control is not None
    host = _local_short_host()
    out: dict[str, Any] = {"kind": "svn", "checkouts": [], "store": "hub" if via_hub else "snapshots"}
    cos = _discover_svn_checkouts(prof)
    if not cos:
        log(f"  {prof.name}: no svn checkouts found under {prof.paths}")
        return out
    failures: list[str] = []
    for c in cos:
        try:
            meta = svs.snapshot_wc(c["path"], snap_dir, name=c["name"], rel=c["rel"],
                                   exclude=prof.exclude)
        except svs.SvnSyncError as e:
            benign = "locked" in str(e) or "conflicted" in str(e)
            out["checkouts"].append({"name": c["name"], "rel": c["rel"],
                                     ("skipped" if benign else "error"): str(e)})
            if not benign:
                failures.append(f"{c['rel']}: {e}")
            log(f"  {prof.name}: {'SKIP' if benign else 'ERROR'} {c['rel']} — {e}")
            continue
        if via_hub:
            try:
                _push_svn_snapshot_to_hub(hub, prof, c["rel"], host, snap_dir, meta)
                control.put_kind_snapshot("svn", prof.name, c["rel"], host, {**meta, "host": host})
            except Exception as e:  # noqa: BLE001 — transport/registry failure for ONE checkout
                out["checkouts"].append({"name": c["name"], "rel": c["rel"], "error": f"hub: {e}"})
                failures.append(f"{c['rel']}: hub: {e}")
                log(f"  {prof.name}: ERROR {c['rel']} — hub: {e}")
                continue
        out["checkouts"].append({"name": c["name"], "rel": c["rel"], "revision": meta["revision"],
                                 "dirty": meta["dirty"], "unversioned": meta["unversioned_count"],
                                 "bytes": meta["bytes"]})
        log(f"  {prof.name}: snapshot {c['rel']} r{meta['revision']}"
            + (" dirty" if meta["dirty"] else "")
            + (f", {meta['unversioned_count']} unversioned" if meta["unversioned_count"] else "")
            + f" ({meta['bytes']}B)")
    if failures:
        raise HomesyncError(f"{prof.name}: {len(failures)} checkout(s) failed to snapshot: "
                            + "; ".join(failures[:3]))
    return out


_SVN_STICKY_BENIGN = {"up to date", "no snapshot", "svn server unreachable"}


def auto_apply_svn_profile(prof: Profile, snap_dir: str | Path, *,
                           run_id: str, dry_run: bool, log,
                           hub: "Hub | None" = None, control=None) -> dict[str, Any]:
    """Receiver side (apply:auto): apply each staged snapshot whose checkout
    belongs to this profile, guarded by ``svnsync.apply_decision``; backs the
    receiver up first. Same sticky/attention semantics as the git auto-apply."""
    snap_dir = Path(snap_dir).expanduser()
    home = _home()
    backup_dir = Path(DEFAULT_GIT_BACKUP_ROOT).expanduser() / f"{run_id}-svnapply"
    state_dir = state_root()
    via_hub = hub is not None and control is not None
    out: dict[str, Any] = {"kind": "svn", "apply": "auto", "status": "ok",
                           "applied": [], "skipped": [], "errors": [],
                           "sticky": [], "warnings": [], "store": "hub" if via_hub else "snapshots"}
    if via_hub:
        try:
            metas = _hub_kind_metas(control, "svn", prof, self_host=_local_short_host())
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"hub registry: {e}")
            out["status"] = "error"
            return out
    else:
        if not snap_dir.is_dir():
            return out
        metas = svs.load_metas(snap_dir)
    streaks = StickyStreaks(f"{prof.name}/", run_id=run_id, dry_run=dry_run)
    for meta in metas:
        rel = meta.get("rel")
        if not rel or not any(rel == b or rel.startswith(b + "/") for b in prof.paths):
            continue
        name = meta.get("name") or Path(rel).name
        target = home / rel
        src_dir = snap_dir
        ok, reason = svs.apply_decision(target, meta, state_dir=state_dir)
        if not ok:
            out["skipped"].append({"checkout": name, "reason": reason})
            suffix = "" if reason in _SVN_STICKY_BENIGN else streaks.note(name, reason)[2]
            log(f"  {prof.name}/{name}: auto-apply skipped ({reason}){suffix}")
            continue
        if dry_run:
            out["applied"].append({"checkout": name, "dry_run": True})
            log(f"  {prof.name}/{name}: would auto-apply r{meta.get('revision')}"
                + (" + diff" if meta.get("dirty") else "")
                + (f" + {meta.get('unversioned_count')} unversioned" if meta.get("unversioned_count") else ""))
            continue
        try:
            if via_hub:
                src_dir = _pull_svn_snapshot_from_hub(hub, meta, snap_dir / "hub" / meta["host"] / name)
            res = svs.apply_wc(target, src_dir, meta, backup_dir=backup_dir, state_dir=state_dir)
            out["applied"].append({"checkout": name, "revision": res["revision"],
                                   "created": res["created"], "backup": res.get("backup")})
            log(f"  {prof.name}/{name}: auto-applied r{res['revision']}"
                + (" (new checkout)" if res["created"] else "")
                + (" + diff" if res["patched"] else "")
                + (f" + {res['unpacked']} unversioned" if res["unpacked"] else ""))
        except svs.SvnServerUnreachable as e:
            reason = "svn server unreachable"
            out["skipped"].append({"checkout": name, "reason": reason})
            log(f"  {prof.name}/{name}: auto-apply skipped ({reason}: {str(e)[:120]})")
        except svs.SvnSyncError as e:
            out["errors"].append(f"{name}: {e}")
            log(f"  {prof.name}/{name}: auto-apply ERROR {e}")
        except Exception as e:  # noqa: BLE001 — one checkout must never take the pass down
            out["errors"].append(f"{name}: unexpected {type(e).__name__}: {e}")
            log(f"  {prof.name}/{name}: auto-apply ERROR unexpected {type(e).__name__}: {e}")
    if out["errors"]:
        out["status"] = "error"
    elif any(s["reason"] not in _SVN_STICKY_BENIGN for s in out["skipped"]):
        out["status"] = "attention"
    streaks.finish()
    out["sticky"].extend(streaks.sticky)
    out["warnings"].extend(streaks.warnings)
    return out


def cmd_svn(args) -> int:
    """`fsync svn snapshot|apply|status|restore` — kind:svn working-copy sync."""
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    action = getattr(args, "svn_cmd", None)
    if action not in ("snapshot", "apply", "status", "restore"):
        print("usage: fsync svn {snapshot|apply|status|restore} [--profile NAME ...] [--all]",
              file=sys.stderr)
        return 2
    try:
        _peer, profiles, _defaults = load_config(getattr(args, "config", None))
    except HomesyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    snap_dir = Path(getattr(args, "snapshot_dir", None) or DEFAULT_SVN_SNAPSHOT_DIR).expanduser()
    svn_profiles = {n: p for n, p in profiles.items() if p.kind == "svn"}
    hub = _peer if isinstance(_peer, Hub) else None
    control = None
    if hub is not None:
        from .hub import hub_control
        if not peer_reachable(hub):
            print(f"{hub.label} not reachable on any address", file=sys.stderr)
            return 1
        try:
            control = hub_control(hub, getattr(args, "config", None))
        except HomesyncError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    if action == "snapshot":
        if not svn_profiles:
            print("no kind:svn profiles in config — add one (see docs/issues-and-fixes.md ISSUE-010)",
                  file=sys.stderr)
            return 2
        want = getattr(args, "profile", None)
        selected = [p for n, p in svn_profiles.items() if not want or n in want]
        if want and len(selected) != len(set(want)):
            print(f"unknown svn profile(s): {sorted(set(want) - set(svn_profiles))}", file=sys.stderr)
            return 2
        total = 0
        rc = 0
        for prof in selected:
            try:
                total += len(snapshot_svn_profile(prof, snap_dir, log, hub=hub, control=control)["checkouts"])
            except HomesyncError as e:
                print(f"error: {e}", file=sys.stderr)
                rc = 1
        print(f"snapshot: {total} checkout(s) -> {hub.label if hub else snap_dir}")
        return rc

    if control is not None:
        metas = []
        for prof in svn_profiles.values():
            metas.extend(_hub_kind_metas(control, "svn", prof, self_host=_local_short_host(),
                                         only=getattr(args, "checkout", None)))
    else:
        metas = svs.load_metas(snap_dir, only=getattr(args, "checkout", None))
    if not metas:
        if control is not None:
            print("no svn snapshots on the hub — run `fsync svn snapshot` on the producer box first",
                  file=sys.stderr)
        else:
            print(f"no svn snapshots in {snap_dir} — run `fsync svn snapshot` on the source box "
                  f"(and let the file-sync carry {snap_dir.name}/) first", file=sys.stderr)
        return 2
    home = _home()
    state_dir = state_root()
    backup_root = Path(getattr(args, "backup_root", None) or DEFAULT_GIT_BACKUP_ROOT).expanduser()

    if action == "status":
        for meta in metas:
            rel = meta.get("rel") or meta["name"]
            prev = svs.preview_apply(home / rel, meta, state_dir=state_dir)
            flag = ("receiver-ahead (STALE incoming)" if prev["receiver_ahead"]
                    else "CHANGE" if prev["would_change"] else f"skip: {prev['reason']}")
            print(f"{meta['name']}: {rel} [{flag}] incoming r{meta.get('revision')}"
                  f"{' dirty' if meta.get('dirty') else ''}"
                  + (f" +{meta.get('unversioned_count')} unversioned" if meta.get("unversioned_count") else "")
                  + (f" (receiver r{prev['receiver_revision']})" if prev["exists"] else "  (new: absent locally)"))
        return 0

    if action == "restore":
        only = getattr(args, "checkout", None)
        from_dir = getattr(args, "from_dir", None)
        rc = 0
        for meta in metas:
            nm = meta["name"]
            if only and nm != only:
                continue
            run = (Path(from_dir).expanduser() if from_dir
                   else _latest_backup_run(backup_root, nm, suffix="-svnapply"))
            if not run or not (run / f"{nm}.pre-apply.meta.json").exists():
                print(f"{nm}: no pre-apply backup found", file=sys.stderr)
                rc = 1
                continue
            try:
                res = svs.restore_backup(home / (meta.get("rel") or nm), run, nm)
            except svs.SvnSyncError as e:
                print(f"{nm}: ERROR {e}", file=sys.stderr)
                rc = 1
                continue
            print(f"{nm}: restored -> r{res['revision']} (from {run.name}, "
                  f"{res['restored_unversioned']} unversioned file(s) put back)")
        return rc

    # apply
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-manual"
    backup_dir = backup_root / f"{run_id}-svnapply"
    rc = 0
    for meta in metas:
        nm = meta["name"]
        target = home / (meta.get("rel") or nm)
        ok, reason = svs.apply_decision(target, meta, state_dir=state_dir)
        forced = (not ok and getattr(args, "force", False)
                  and reason == "receiver has local changes")
        if not ok and not forced:
            print(f"{nm}: skipped ({reason})")
            continue
        try:
            if forced:
                saved = svs.force_clean(target, backup_dir, nm)
                print(f"{nm}: receiver's local changes saved to {saved} and reverted (--force)")
            src_dir = (_pull_svn_snapshot_from_hub(hub, meta, snap_dir / "hub" / meta["host"] / nm)
                       if hub is not None else snap_dir)
            res = svs.apply_wc(target, src_dir, meta, backup_dir=backup_dir, state_dir=state_dir)
        except svs.SvnSyncError as e:
            print(f"{nm}: ERROR {e}", file=sys.stderr)
            rc = 1
            continue
        print(f"{nm}: applied r{res['revision']}" + (" (new checkout)" if res["created"] else "")
              + (" + diff" if res["patched"] else "")
              + (f" + {res['unpacked']} unversioned" if res["unpacked"] else "")
              + (f"  [backup {backup_dir}]" if res.get("backup") else ""))
    keep = getattr(args, "keep", 10)
    if keep:
        svs.prune_backup_runs(backup_root, keep=keep)
    return rc


def snapshot_git_profile(prof: Profile, bundle_dir: str | Path, log, *,
                         hub: "Hub | None" = None, control=None) -> dict[str, Any]:
    """Producer side: capture every repo in the profile — into the hub's bare
    repos (P7.3, R39: ``refs/fsync/<host>/…`` + a meta the hub registers) when a
    hub control plane is given, else into ``bundle_dir`` as bundle files."""
    via_hub = hub is not None and control is not None
    bundle_dir = Path(bundle_dir).expanduser()
    if not via_hub:
        bundle_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"kind": "git", "repos": [], "store": "hub" if via_hub else "bundles"}
    host = _local_short_host()
    repos = _discover_git_repos(prof)
    if not repos:
        log(f"  {prof.name}: no git repos found under {prof.paths}")
        return out
    # Per-repo isolation: one repo that can't be captured (busy, unborn, or a
    # genuine git error) must NOT abort the loop — every other repo in the
    # profile still needs to be backed up. Real failures are collected and
    # re-raised once at the end so the run still reports rc!=0 (and notifies),
    # but an unborn/empty repo is a benign skip, not a failure to alarm on.
    failures: list[str] = []
    for r in repos:
        busy = grs.repo_busy(r["path"])
        if busy:
            out["repos"].append({"name": r["name"], "rel": r["rel"], "skipped": busy})
            log(f"  {prof.name}: SKIP {r['rel']} — {busy}")
            continue
        if not grs.has_commits(r["path"]):
            reason = "no commits yet (unborn HEAD) — nothing to capture"
            out["repos"].append({"name": r["name"], "rel": r["rel"], "skipped": reason})
            log(f"  {prof.name}: SKIP {r['rel']} — {reason}")
            continue
        try:
            if via_hub:
                from .hub import ensure_hub_bare, hub_git_url
                ensure_hub_bare(hub, r["rel"])
                meta = grs.snapshot_repo_to_hub(r["path"], hub_git_url(hub, r["rel"]), host,
                                                name=r["name"], rel=r["rel"])
                control.put_kind_snapshot("git", prof.name, r["rel"], host, meta)
            else:
                meta = grs.snapshot_repo(r["path"], bundle_dir, name=r["name"], rel=r["rel"])
        except grs.GitSyncError as e:
            out["repos"].append({"name": r["name"], "rel": r["rel"], "error": str(e)})
            failures.append(f"{r['rel']}: {e}")
            log(f"  {prof.name}: ERROR {e}")
            continue
        except Exception as e:  # noqa: BLE001 — hub registry/transport failure for ONE repo
            out["repos"].append({"name": r["name"], "rel": r["rel"], "error": str(e)})
            failures.append(f"{r['rel']}: hub: {e}")
            log(f"  {prof.name}: ERROR {r['rel']}: hub: {e}")
            continue
        out["repos"].append({"name": r["name"], "rel": r["rel"], "branch": meta["branch"],
                             "dirty": meta["dirty"], "bytes": meta.get("bytes", 0)})
        log(f"  {prof.name}: snapshot {r['rel']}"
            + (f" -> hub {meta['branch'] or 'DETACHED'}@{meta['head'][:9]}" if via_hub
               else f" ({meta['bytes']}B)")
            + (", dirty" if meta["dirty"] else ""))
        for w in grs.special_warnings(r["path"]):
            log(f"  {prof.name}: warn {r['rel']}: {w}")
    if failures:
        raise grs.GitSyncError(f"{len(failures)} repo(s) failed to snapshot: "
                               + "; ".join(failures))
    return out


def _select_git_profiles(profiles: dict[str, Profile], args) -> list[Profile] | None:
    if getattr(args, "profile", None):
        sel = []
        for n in args.profile:
            if n not in profiles:
                print(f"unknown git profile: {n} (available: {', '.join(profiles) or 'none'})",
                      file=sys.stderr)
                return None
            sel.append(profiles[n])
        return sel
    return list(profiles.values())  # default + --all: every git profile


def _hub_kind_metas(control, kind: str, prof: Profile, *, self_host: str,
                    only: str | None = None) -> list[dict]:
    """P7.3 receiver side: for each repo/checkout of ``prof`` the NEWEST snapshot
    another box registered on the hub (never our own — a producer does not
    apply itself). Each meta carries ``host`` (its namespace / store dir)."""
    best: dict[str, dict] = {}
    for row in control.kind_snapshots(kind, prof.name):
        if row["host"] == self_host:
            continue
        meta = dict(row["meta"] or {})
        meta.setdefault("rel", row["rel"])
        meta.setdefault("name", row.get("name") or Path(row["rel"]).name)
        meta["host"] = row["host"]
        meta["ts"] = row["ts"]
        if only and meta.get("name") != only:
            continue
        cur = best.get(row["rel"])
        if cur is None or row["ts"] > cur["ts"]:
            best[row["rel"]] = meta
    return [best[k] for k in sorted(best)]


def _load_bundle_metas(bundle_dir: Path, only: str | None = None) -> list[dict]:
    if not bundle_dir.exists():
        return []
    metas = []
    for mp in sorted(bundle_dir.glob("*.meta.json")):
        try:
            meta = json.loads(mp.read_text())
        except (OSError, ValueError):
            continue
        if only and meta.get("name") != only:
            continue
        metas.append(meta)
    return metas


def _latest_backup_run(backup_root: Path, name: str, suffix: str = "-git") -> Path | None:
    """Newest ``*<suffix>`` backup-run dir that contains a pre-apply backup for
    ``name`` (timestamp-prefixed names sort chronologically)."""
    if not backup_root.exists():
        return None
    for d in sorted((d for d in backup_root.iterdir()
                     if d.is_dir() and d.name.endswith(suffix)), reverse=True):
        if (d / f"{name}.pre-apply.meta.json").exists():
            return d
    return None


def cmd_git(args) -> int:
    """`fsync git snapshot|apply|status` — full-fidelity git-repo sync."""
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    action = getattr(args, "git_cmd", None)
    if action not in ("snapshot", "apply", "status", "restore"):
        print("usage: fsync git {snapshot|apply|status|restore} [--profile NAME ...] [--all]",
              file=sys.stderr)
        return 2
    try:
        _peer, profiles, _defaults = load_config(getattr(args, "config", None))
    except HomesyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    bundle_dir = Path(getattr(args, "bundle_dir", None) or DEFAULT_GIT_BUNDLE_DIR).expanduser()
    hub = _peer if isinstance(_peer, Hub) else None
    control = None
    if hub is not None:
        from .hub import hub_control, hub_git_url
        if not peer_reachable(hub):
            print(f"{hub.label} not reachable on any address", file=sys.stderr)
            return 1
        try:
            control = hub_control(hub, getattr(args, "config", None))
        except HomesyncError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    git_profiles = {n: p for n, p in profiles.items() if p.kind == "git"}

    if action == "snapshot":
        if not git_profiles:
            print("no kind:git profiles in config — add one (see docs/git-repo-sync.md)",
                  file=sys.stderr)
            return 2
        selected = _select_git_profiles(git_profiles, args)
        if selected is None:
            return 2
        total = 0
        try:
            for prof in selected:
                total += len(snapshot_git_profile(prof, bundle_dir, log, hub=hub, control=control)["repos"])
        except grs.GitSyncError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"snapshot: {total} repo(s) -> {hub.label if hub else bundle_dir}")
        return 0

    # apply / status operate on the snapshots the hub registered for other boxes
    # (P7.3), or on the bundles present in bundle_dir (v1 carriers).
    sources: dict[str, tuple[str, str | None]] = {}
    if control is not None:
        metas = []
        for prof in git_profiles.values():
            for m in _hub_kind_metas(control, "git", prof, self_host=_local_short_host(),
                                     only=getattr(args, "repo", None)):
                metas.append(m)
                sources[m["name"]] = (hub_git_url(hub, m["rel"]), m.get("namespace") or grs.hub_namespace(m["host"]))
    else:
        metas = _load_bundle_metas(bundle_dir, only=getattr(args, "repo", None))
        for m in metas:
            sources[m["name"]] = (str(bundle_dir / f"{m['name']}.bundle"), None)
    if not metas:
        where = f" matching {args.repo}" if getattr(args, "repo", None) else ""
        if control is not None:
            print(f"no git snapshots on the hub{where} — run `fsync git snapshot` on the producer box first",
                  file=sys.stderr)
        else:
            print(f"no bundles in {bundle_dir}{where} — run `fsync git snapshot` on the "
                  f"source box (and let the file-sync carry {bundle_dir.name}/) first",
                  file=sys.stderr)
        return 2

    home = _home()

    if action == "restore":
        backup_root = Path(getattr(args, "backup_root", None) or DEFAULT_GIT_BACKUP_ROOT).expanduser()
        only = getattr(args, "repo", None)
        from_dir = getattr(args, "from_dir", None)
        targets: list[tuple[str, Path]] = []
        if from_dir:
            run = Path(from_dir).expanduser()
            for mp in sorted(run.glob("*.pre-apply.meta.json")):
                nm = json.loads(mp.read_text()).get("name") or mp.name.split(".pre-apply")[0]
                if not only or nm == only:
                    targets.append((nm, run))
        else:
            names = [only] if only else [m["name"] for m in metas]
            for nm in names:
                run = _latest_backup_run(backup_root, nm)
                if run:
                    targets.append((nm, run))
        if not targets:
            print("no pre-apply backups found to restore", file=sys.stderr)
            return 2
        rc = 0
        for nm, run in targets:
            meta = json.loads((run / f"{nm}.pre-apply.meta.json").read_text())
            rel = meta.get("rel") or nm
            try:
                res = grs.restore_backup(home / rel, run, nm)
            except grs.GitSyncError as e:
                print(f"{nm}: ERROR {e}", file=sys.stderr)
                rc = 1
                continue
            print(f"{nm}: restored -> {rel} (from {run.name}, mode {res['mode']})")
        return rc

    if action == "status":
        for meta in metas:
            rel = meta.get("rel") or meta["name"]
            prev = grs.preview_apply(home / rel, meta)
            flag = ("receiver-ahead (STALE incoming)" if prev.get("receiver_ahead")
                    else "CHANGE" if prev["would_change"] else "up-to-date")
            newmark = "" if prev["exists"] else "  (new: absent locally)"
            print(f"{meta['name']}: {rel} [{flag}] incoming "
                  f"{meta['branch'] or 'DETACHED'}@{meta['head'][:9]}"
                  f"{' dirty' if meta['dirty'] else ''}{newmark}")
        return 0

    # apply
    backup_root = Path(getattr(args, "backup_root", None) or DEFAULT_GIT_BACKUP_ROOT).expanduser()
    backup_dir = backup_root / (time.strftime("%Y%m%d-%H%M%S") + "-git")
    rc = 0
    for meta in metas:
        rel = meta.get("rel") or meta["name"]
        source, namespace = sources[meta["name"]]
        target = home / rel
        # Never mutate a receiver repo that has a git operation in flight —
        # applying would race a running git or clobber a merge/rebase.
        if grs.is_git_repo(target):
            busy = grs.repo_busy(target)
            if busy:
                print(f"{meta['name']}: SKIP {rel} — receiver {busy}", file=sys.stderr)
                rc = 1
                continue
            # Fix B (ISSUE-001): refuse to apply across a branch mismatch unless
            # forced — apply would silently switch the receiver's branch and can
            # leave producer-tracked files untracked here.
            div = grs.check_branch_divergence(target, meta)
            if div and not getattr(args, "force", False):
                print(f"{meta['name']}: SKIP {rel} — branch divergence: receiver on "
                      f"{div['receiver_branch'] or 'DETACHED'}, producer on "
                      f"{div['producer_branch'] or 'DETACHED'}. Applying would switch this "
                      f"repo's branch and can leave producer-tracked files untracked. "
                      f"Re-run with --force to proceed (receiver is backed up first).",
                      file=sys.stderr)
                rc = 1
                continue
            if div:
                log(f"{meta['name']}: WARNING branch divergence — receiver "
                    f"{div['receiver_branch'] or 'DETACHED'}, producer "
                    f"{div['producer_branch'] or 'DETACHED'} — proceeding under --force")
            # ISSUE-006 sub-finding: same refusal shape as divergence — a stale
            # bundle must not roll the receiver back. (apply_repo re-checks
            # post-fetch, which also catches heads unknown to this heuristic.)
            ahead = grs.check_receiver_ahead(target, meta)
            if ahead and not getattr(args, "force", False):
                print(f"{meta['name']}: SKIP {rel} — receiver is ahead: incoming "
                      f"{ahead['incoming_head'][:9]} is an ancestor of receiver "
                      f"{ahead['receiver_head'][:9]}. Applying would roll receiver "
                      f"commits off the branch (stale bundle — re-snapshot the "
                      f"producer). Re-run with --force to proceed (receiver is "
                      f"backed up first).",
                      file=sys.stderr)
                rc = 1
                continue
            if ahead:
                log(f"{meta['name']}: WARNING receiver ahead of incoming "
                    f"({ahead['incoming_head'][:9]} ancestor of "
                    f"{ahead['receiver_head'][:9]}) — proceeding under --force")
        try:
            res = grs.apply_repo(target, source, meta, backup_dir=backup_dir,
                                 mirror_branches=getattr(args, "mirror_branches", False),
                                 force=getattr(args, "force", False), namespace=namespace)
        except grs.GitSyncError as e:
            print(f"{meta['name']}: ERROR {e}", file=sys.stderr)
            rc = 1
            continue
        print(f"{meta['name']}: applied -> {rel} ({meta['branch'] or 'DETACHED'}"
              f"@{meta['head'][:9]}{', dirty' if meta['dirty'] else ''}"
              f"{', created' if res['created'] else ''})"
              + (f"  backup {res['backup']}" if res["backup"] else "")
              + (f"  ({res['saved_ignored']} ignored saved)" if res.get("saved_ignored") else ""))
    pruned = grs.prune_backup_runs(backup_root, keep=getattr(args, "keep", 10))
    log(f"apply: receiver backups under {backup_dir}"
        + (f" (pruned {pruned} old run(s))" if pruned else ""))
    log(f"undo with: fsync git restore{' --repo ' + args.repo if getattr(args, 'repo', None) else ''}")
    return rc


# --------------------------------------------------------------------------- #
# Meta-synchronization: keep the sync TOOLING (fsync code + config) coherent    #
# across both boxes. Code propagates via the editable install + the repo's own  #
# file profile; this adds config `include:` propagation and a `requires:`        #
# compatibility gate so a box never acts on config its build doesn't support.    #
# --------------------------------------------------------------------------- #

def _resolve_raw(config_path: str | None) -> dict:
    import yaml
    cfg = Path(config_path or DEFAULT_CONFIG).expanduser()
    if not cfg.exists():
        return {}
    return _resolve_config(cfg, yaml)


def build_manifest(config_path: str | None = None) -> dict[str, Any]:
    """This box's fsync identity for meta comparison: version, capabilities, and
    a config summary. Never raises — a bad/missing config is reported inline."""
    m: dict[str, Any] = {"fsync_version": FSYNC_VERSION, "features": sorted(FEATURES),
                         "host": socket.gethostname()}
    try:
        import yaml
        cfg = Path(config_path or DEFAULT_CONFIG).expanduser()
        includes = (yaml.safe_load(cfg.read_text()) or {}).get("include") or [] if cfg.exists() else []
        raw = _resolve_raw(config_path)
        peer, profiles, _d = load_config(config_path)
        m["config"] = {
            "ok": True,
            "peer": peer.target,
            "requires": sorted(raw.get("requires") or []),
            "includes": [str(x) for x in includes],
            "profiles": {n: {"kind": p.kind, "direction": p.direction}
                         for n, p in profiles.items()},
            "git_profiles": [n for n, p in profiles.items() if p.kind == "git"],
        }
    except HomesyncError as e:
        m["config"] = {"ok": False, "error": str(e)}
    return m


def _meta_status(config_path: str | None) -> int:
    local = build_manifest(config_path)
    print(f"local : fsync {local['fsync_version']}  features={','.join(local['features'])}")
    try:
        peer, _profiles, _d = load_config(config_path)
    except HomesyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    # Probe the peer's INSTALLED fsync (not the pushed engine) so we see its real
    # capability level. An old build lacks `meta` -> reported as needing upgrade.
    try:
        probe = _ssh(peer, "fsync meta version 2>/dev/null || "
                           "~/.local/bin/fsync meta version 2>/dev/null", timeout=30)
    except HomesyncError as e:
        print(f"peer  : {peer.target} — {e}", file=sys.stderr)
        return 1
    peer_m = None
    if probe.returncode == 0 and probe.stdout.strip():
        try:
            peer_m = json.loads(probe.stdout)
        except ValueError:
            peer_m = None
    if not peer_m:
        print(f"peer  : {peer.target} — fsync meta unavailable (old build or unreachable); "
              f"upgrade peer before relying on features it lacks", file=sys.stderr)
        return 1
    print(f"peer  : fsync {peer_m.get('fsync_version')}  "
          f"features={','.join(peer_m.get('features', []))}")
    local_req = set((local.get("config") or {}).get("requires") or [])
    peer_req = set((peer_m.get("config") or {}).get("requires") or [])
    peer_feat = set(peer_m.get("features") or [])
    local_feat = set(local["features"])
    if local["fsync_version"] != peer_m.get("fsync_version"):
        print(f"note  : version drift local={local['fsync_version']} "
              f"peer={peer_m.get('fsync_version')}")
    problems = []
    if local_req - peer_feat:
        problems.append(f"peer missing {sorted(local_req - peer_feat)} that local config requires")
    if peer_req - local_feat:
        problems.append(f"local missing {sorted(peer_req - local_feat)} that peer config requires")
    if problems:
        for p in problems:
            print(f"INCOMPAT: {p}", file=sys.stderr)
        return 1
    print("compatible: both boxes satisfy each other's config requirements")
    return 0


def cmd_meta(args) -> int:
    """`fsync meta version|check|status` — capability + config compatibility."""
    action = getattr(args, "meta_cmd", None)
    config_path = getattr(args, "config", None)
    if action == "version":
        print(json.dumps(build_manifest(config_path), indent=2))
        return 0
    if action == "check":
        m = build_manifest(config_path)
        cfg = m.get("config", {})
        print(f"fsync {m['fsync_version']} on {m['host']}  features={','.join(m['features'])}")
        if not cfg.get("ok"):
            print(f"config: NOT OK — {cfg.get('error')}", file=sys.stderr)
            return 1
        print(f"config: OK  peer={cfg['peer']}  requires={cfg['requires'] or '-'}"
              + (f"  includes={cfg['includes']}" if cfg["includes"] else ""))
        for n, info in cfg["profiles"].items():
            print(f"  {n}: kind={info['kind']} direction={info['direction']}")
        return 0
    if action == "status":
        return _meta_status(config_path)
    print("usage: fsync meta {version|check|status} [--config PATH]", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- #
# Meta-deploy (P6): build the fsync pyz and push it + the multi-host config to  #
# remotes over ssh. Build is a minis-only (source-of-truth) operation; the pyz  #
# runs headless on remotes from a bare python3. Docs: docs/meta-deploy.md.      #
# --------------------------------------------------------------------------- #

def _rsync_push(src: str, target: str, dst_rel: str) -> None:
    """rsync a local file to target:$HOME/<dst_rel> over the trusted ssh."""
    from .meta_deploy import DeployError
    proc = subprocess.run(
        ["rsync", "-a", "-e", "ssh " + " ".join(SSH_OPTS), src, f"{target}:{dst_rel}"],
        capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise DeployError(f"rsync to {target}:{dst_rel} failed: {proc.stderr.strip()}")


def _deploy_to(target: str, pyz: str, cfg: str) -> dict:
    """Install the pyz + config on one remote as an atomic PAIR (R24):

    1. strict backup-first — an absent current file is fine (first deploy), but
       a FAILED cp aborts before anything is overwritten (never swallow it: a
       later rollback would have no valid .bak);
    2. push both to temp, chmod+mv both into place;
    3. verify with `meta version` — and on ANY post-overwrite failure (partial
       mv or failed verify) restore the pair from the .bak copies (cp, not mv:
       the .bak stays on disk for inspection either way)."""
    from . import meta_deploy as md
    binp, cfgp = md.REMOTE_BIN_REL, md.REMOTE_CONFIG_REL
    _ssh_to(target, "mkdir -p ~/.local/bin ~/.config/fsync", timeout=30)
    bk = _ssh_to(target,
                 f"if [ -e ~/{binp} ]; then cp -f ~/{binp} ~/{binp}.bak; fi && "
                 f"if [ -e ~/{cfgp} ]; then cp -f ~/{cfgp} ~/{cfgp}.bak; fi",
                 timeout=30)
    if bk.returncode != 0:
        raise md.DeployError(f"{target}: backup failed — aborting before overwrite: "
                             f"{bk.stderr.strip()}")
    _rsync_push(pyz, target, f"{binp}.tmp")
    _rsync_push(cfg, target, f"{cfgp}.tmp")
    restore = (f"if [ -e ~/{binp}.bak ]; then cp -f ~/{binp}.bak ~/{binp}; fi; "
               f"if [ -e ~/{cfgp}.bak ]; then cp -f ~/{cfgp}.bak ~/{cfgp}; fi")
    inst = _ssh_to(target, f"chmod +x ~/{binp}.tmp && mv ~/{binp}.tmp ~/{binp} && "
                           f"mv ~/{cfgp}.tmp ~/{cfgp}", timeout=30)
    if inst.returncode != 0:
        rb = _ssh_to(target, restore, timeout=30)
        raise md.DeployError(
            f"{target}: install failed ({inst.stderr.strip()}); "
            + ("rolled back from .bak" if rb.returncode == 0
               else "ROLLBACK ALSO FAILED — inspect *.bak on the remote"))
    ver = _ssh_to(target, f"~/{binp} meta version", timeout=30)
    ok = ver.returncode == 0 and "fsync_version" in ver.stdout
    rolled_back = False
    if not ok:
        rb = _ssh_to(target, restore, timeout=30)
        rolled_back = rb.returncode == 0
    return {"target": target, "verify_rc": 0 if ok else (ver.returncode or 1),
            "verify": ver.stdout.strip(), "rolled_back": rolled_back}


def _rsync_dir_push(src_dir: str, target: str, dst_rel: str) -> None:
    """rsync a local directory as an exact mirror to target:$HOME/<dst_rel>/."""
    from .meta_deploy import DeployError
    proc = subprocess.run(
        ["rsync", "-a", "--delete", "-e", "ssh " + " ".join(SSH_OPTS),
         str(src_dir).rstrip("/") + "/", f"{target}:{dst_rel}/"],
        capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise DeployError(f"rsync to {target}:{dst_rel} failed: {proc.stderr.strip()}")


def _daemon_deploy_script() -> str:
    """POSIX script that refreshes one box's fsyncd venv from its wheelhouse.

    Build at <venv>.new and verify the daemon import chain BEFORE touching the
    live venv; then swap (previous venv -> .bak) — a moved venv keeps working
    for `bin/python3 -m ...` because pyvenv.cfg is found relative to the
    invoked interpreter path (only console-script shebangs go stale, and
    nothing uses them). `daemon install` run from the new venv re-renders the
    unit's ExecStart from sys.executable. If the daemon doesn't come back up,
    restore .bak (same path, so the unit needs no change) and restart."""
    from . import meta_deploy as md
    return f'''set -e
V="$HOME/{md.DAEMON_VENV_REL}"
WH="$HOME/{md.WHEELHOUSE_REL}"
rm -rf "$V.new"
if ! (python3 -m venv "$V.new" \\
      && "$V.new/bin/python3" -m pip install --quiet --no-index --find-links "$WH" fsync \\
      && "$V.new/bin/python3" -c "import fsync.daemon"); then
  rm -rf "$V.new"; echo "daemon-deploy: build-failed"; exit 1
fi
rm -rf "$V.bak"
if [ -d "$V" ]; then mv "$V" "$V.bak"; fi
mv "$V.new" "$V"
if "$V/bin/python3" -m fsync.cli daemon install >/dev/null \\
   && systemctl --user restart fsync-daemon.service \\
   && sleep 2 && systemctl --user is-active --quiet fsync-daemon.service; then
  echo "daemon-deploy: ok"
else
  rm -rf "$V.failed"; mv "$V" "$V.failed" 2>/dev/null || true
  if [ -d "$V.bak" ]; then mv "$V.bak" "$V"; systemctl --user restart fsync-daemon.service || true; fi
  echo "daemon-deploy: rolled-back"; exit 1
fi
'''


def _daemon_deploy_to(target: str | None, wheelhouse: str) -> dict:
    """Refresh one box's fsyncd venv from the wheelhouse (target None = this
    box: same script via local bash, no ship). Remotes get the wheelhouse
    mirrored first so the venv install is fully offline."""
    from . import meta_deploy as md
    script = _daemon_deploy_script()
    if target is None:
        proc = subprocess.run(["bash", "-c", script],
                              capture_output=True, text=True, timeout=600)
    else:
        _ssh_to(target, f"mkdir -p ~/{md.WHEELHOUSE_REL}", timeout=30)
        _rsync_dir_push(wheelhouse, target, md.WHEELHOUSE_REL)
        proc = _ssh_to(target, script, timeout=600)
    ok = "daemon-deploy: ok" in proc.stdout
    return {"ok": ok,
            "rolled_back": "daemon-deploy: rolled-back" in proc.stdout,
            "detail": ("venv build/import failed (live venv untouched)"
                       if "daemon-deploy: build-failed" in proc.stdout
                       else "" if ok else f"rc={proc.returncode} {proc.stderr.strip()[:200]}")}


def _deploy_daemon(args) -> int:
    """`fsync deploy daemon` — rebuild the wheelhouse from source, then refresh
    the standalone fsyncd venv on this box + every remote (or --local / --host
    subset), reinstall units, restart daemons. Per-box failures roll back to
    the previous venv and don't strand the rest of the fleet."""
    from . import meta_deploy as md
    dry = getattr(args, "dry_run", False)
    local_only = getattr(args, "local", False)
    only = getattr(args, "host", None)
    remotes: list[tuple[str, str]] = []
    if not local_only:
        config_path = getattr(args, "config", None)
        cfg_file = Path(config_path or DEFAULT_CONFIG).expanduser()
        try:
            raw = _resolve_raw(config_path)
        except HomesyncError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if "hosts" not in raw:
            print(f"{cfg_file}: not a multi-host config (no hosts:) — needed to "
                  f"find remotes (or use --local)", file=sys.stderr)
            return 2
        try:
            remotes = md.remote_targets(raw, socket.gethostname())
        except md.DeployError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if only:
            remotes = [(hk, t) for hk, t in remotes if hk == only]
            if not remotes:
                print(f"unknown or self host: {only}", file=sys.stderr)
                return 2
    # --host targets just that remote; otherwise this box is refreshed too
    plan: list[tuple[str, str | None]] = ([] if only else [("local", None)]) + remotes
    if dry:
        for hk, tgt in plan:
            where = "this box" if tgt is None else tgt
            print(f"[dry-run] {hk} ({where}): wheelhouse -> ~/{md.WHEELHOUSE_REL}, "
                  f"rebuild ~/{md.DAEMON_VENV_REL}, reinstall unit, restart fsyncd")
        return 0
    try:
        wh = md.build_wheelhouse()
    except md.DeployError as e:
        print(f"error: wheelhouse build failed: {e}", file=sys.stderr)
        return 1
    print(f"wheelhouse: {wh['path']} ({len(wh['wheels'])} wheels)", file=sys.stderr)
    rc = 0
    for hk, tgt in plan:
        try:
            res = _daemon_deploy_to(tgt, wh["path"])
        except (md.DeployError, HomesyncError, subprocess.SubprocessError, OSError) as e:
            print(f"{hk}: ERROR {e}", file=sys.stderr)
            rc = 1
            continue
        if res["ok"]:
            print(f"{hk}: OK (fsyncd active on the refreshed venv)")
        else:
            rc = 1
            print(f"{hk}: FAILED — "
                  + ("rolled back to the previous venv" if res["rolled_back"]
                     else res["detail"]), file=sys.stderr)
    return rc


def cmd_deploy(args) -> int:
    """`fsync deploy build|push|status|daemon` — build the pyz, push it + the
    multi-host config to remotes over ssh, or refresh the fsyncd venvs."""
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)
    from . import meta_deploy as md

    action = getattr(args, "deploy_cmd", None)
    if action not in ("build", "push", "status", "daemon"):
        print("usage: fsync deploy {build|push|status|daemon} [--config PATH]", file=sys.stderr)
        return 2

    if action == "daemon":
        return _deploy_daemon(args)

    if action == "build":
        try:
            res = md.build_pyz(getattr(args, "out", None) or md.DEFAULT_PYZ)
        except md.DeployError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"built {res['path']} ({res['bytes']} bytes, {len(res['modules'])} core modules)")
        return 0

    # push / status resolve remotes from the multi-host config
    config_path = getattr(args, "config", None)
    cfg_file = Path(config_path or DEFAULT_CONFIG).expanduser()
    try:
        raw = _resolve_raw(config_path)
    except HomesyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if "hosts" not in raw:
        print(f"{cfg_file}: not a multi-host config (no hosts:) — deploy needs one "
              f"(see docs/meta-deploy.md)", file=sys.stderr)
        return 2
    try:
        targets = md.remote_targets(raw, socket.gethostname())
    except md.DeployError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    only = getattr(args, "host", None)
    if only:
        targets = [(hk, t) for hk, t in targets if hk == only]
        if not targets:
            print(f"unknown or self host: {only}", file=sys.stderr)
            return 2
    if not targets:
        print("no remote hosts to deploy to (only self in config)", file=sys.stderr)
        return 0

    if action == "status":
        rc = 0
        for hk, tgt in targets:
            try:
                p = _ssh_to(tgt, f"~/{md.REMOTE_BIN_REL} meta version 2>/dev/null || "
                                 f"fsync meta version 2>/dev/null", timeout=30)
            except (subprocess.SubprocessError, OSError, HomesyncError):
                print(f"{hk} ({tgt}): unreachable (ssh timed out)", file=sys.stderr)
                rc = 1
                continue
            if p.returncode == 0 and '"fsync_version"' in p.stdout:
                try:
                    m = json.loads(p.stdout)
                    print(f"{hk} ({tgt}): fsync {m.get('fsync_version')} "
                          f"features={','.join(m.get('features', []))}")
                except ValueError:
                    print(f"{hk} ({tgt}): unparseable meta output", file=sys.stderr)
                    rc = 1
            else:
                print(f"{hk} ({tgt}): unreachable or no deployed fsync", file=sys.stderr)
                rc = 1
        return rc

    # push
    if not cfg_file.exists():
        print(f"config not found: {cfg_file}", file=sys.stderr)
        return 2
    dry = getattr(args, "dry_run", False)
    pyz = None
    if not dry:
        try:
            pyz = md.build_pyz(getattr(args, "out", None) or md.DEFAULT_PYZ)["path"]
        except md.DeployError as e:
            print(f"error: build failed: {e}", file=sys.stderr)
            return 1
    rc = 0
    for hk, tgt in targets:
        if dry:
            print(f"[dry-run] {hk} ({tgt}): push pyz -> ~/{md.REMOTE_BIN_REL}, "
                  f"{cfg_file.name} -> ~/{md.REMOTE_CONFIG_REL}")
            continue
        log(f"deploying to {hk} ({tgt}) ...")
        try:
            res = _deploy_to(tgt, pyz, str(cfg_file))
        except (md.DeployError, HomesyncError, subprocess.SubprocessError, OSError) as e:
            # keep going: one slow/failed host must not strand the rest of the
            # fleet un-attempted (a mid-install failure already rolled back)
            print(f"{hk}: ERROR {e}", file=sys.stderr)
            rc = 1
            continue
        ok = res["verify_rc"] == 0 and "fsync_version" in res["verify"]
        if ok:
            print(f"{hk} ({tgt}): OK (verified)")
        else:
            rc = 1
            print(f"{hk} ({tgt}): verify FAILED — "
                  + ("rolled back to the previous install"
                     if res.get("rolled_back")
                     else "ROLLBACK ALSO FAILED (inspect *.bak on the remote)"),
                  file=sys.stderr)
            log(f"  verify output: {res['verify'][:200]}")
    # P7.3: publish the same pyz + config on the hub so a spoke that missed this
    # push (laptop away) can `fsync self-update` the next time it sees the hub.
    if not dry and not getattr(args, "no_publish", False):
        try:
            peer_cfg, _, _ = load_config(config_path)
        except HomesyncError:
            peer_cfg = None
        if isinstance(peer_cfg, Hub) and (peer_cfg.local or peer_reachable(peer_cfg)):
            from .hub import publish_artifact
            try:
                man = publish_artifact(peer_cfg, pyz, str(cfg_file))
                print(f"hub {peer_cfg.host}: artifact {man['version']} published")
            except (HomesyncError, OSError, subprocess.SubprocessError) as e:
                print(f"hub artifact publish FAILED: {e}", file=sys.stderr)
                rc = 1
    return rc


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
        from dataclasses import replace

        selected = []
        for spec in args.profile:
            # "name" honors the profile's configured direction;
            # "name=push|pull|both" overrides it for this run (TUI cycle).
            name, _, direction = spec.partition("=")
            if name not in profiles:
                print(f"unknown profile: {name} (available: {', '.join(profiles)})", file=sys.stderr)
                return 2
            if direction and direction not in DIRECTIONS:
                print(f"{spec}: direction must be one of {DIRECTIONS}", file=sys.stderr)
                return 2
            prof = profiles[name]
            selected.append(replace(prof, direction=direction) if direction else prof)
    else:
        print(f"pick profiles with --profile NAME[=push|pull|both] (repeatable) or --all; "
              f"available: {', '.join(profiles)}", file=sys.stderr)
        return 2

    # Fix C: when a kind:git profile is apply:auto, stop the file profiles from
    # double-carrying its repos (ISSUE-001 ghost transport). Computed from ALL
    # config profiles so the exclusion holds even in a partial run.
    _extra_excludes = git_repo_excludes_for_file_profiles(profiles)
    if _extra_excludes:
        from dataclasses import replace
        selected = [replace(p, exclude=list(p.exclude) + _extra_excludes[p.name])
                    if p.name in _extra_excludes else p for p in selected]
        for _n, _pats in _extra_excludes.items():
            log(f"note: {_n}: {len(_pats)} git-profile repo tree(s) excluded from file "
                f"sync (apply:auto)")

    # P7.1: a hub-only box (role: [hub], e.g. a file server with no carry-set
    # of its own) serves the spokes and never syncs its own home — an additive
    # two-way run from an empty home would pull the whole hub tree into it.
    if isinstance(peer, Hub) and "spoke" not in peer.roles:
        if getattr(args, "plan_only", False):
            print(json.dumps({"peer_reachable": True, "peer": peer.label, "hub_only": True,
                              "profiles": {}}))
            return 0
        print(f"this box is hub-only ({peer.label}, role {peer.roles}) — nothing to sync here; "
              f"spokes run `fsync sync run` against it, see `fsync hub status`", file=sys.stderr)
        return 0

    # R6: away-tolerant — a missing peer is a clean no-op, not an error.
    if not peer_reachable(peer):
        if getattr(args, "plan_only", False):
            print(json.dumps({"peer_reachable": False, "peer": peer.target}))
            return 0
        print(f"{peer.label} not reachable on any address — nothing to do", file=sys.stderr)
        return 0

    if getattr(args, "plan_only", False):
        # Preview for a UI: JSON on stdout, no lock, nothing transferred.
        try:
            ensure_peer_engine(peer)
            pv_control = None
            if isinstance(peer, Hub):
                from .hub import hub_control
                pv_control = hub_control(peer, getattr(args, "config", None))
            preview = build_run_preview(peer, selected, workers=args.workers,
                                        progress_path=getattr(args, "plan_progress", None),
                                        control=pv_control)
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
        # A normal overlap (timer vs TUI/manual run), not a failure — exit 0
        # so a timer-triggered service doesn't land in the failed state.
        print("another `fsync sync run` is already in progress — deferring, nothing done",
              file=sys.stderr)
        return 0

    control = None
    if isinstance(peer, Hub):
        # P7.1 (R36): per-profile leases on the hub replace the whole-box busy
        # probe. Taken inside the loop, so one profile in flight elsewhere
        # defers only itself; the hub's control plane being down is an error
        # for every file profile, never a silent pairwise fallback.
        from .hub import hub_control
        try:
            control = hub_control(peer, getattr(args, "config", None))
        except HomesyncError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    # Cross-box guard: if the peer is driving a sync right now, defer cleanly.
    # (Local lock is already held, so the peer's own probe of us backs off.)
    elif peer_busy(peer):
        print(f"peer {peer.target} is running a sync — deferring, nothing done", file=sys.stderr)
        return 0

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    run_dir = st_root / "runs" / run_id
    run_dir.mkdir(parents=True)
    report: dict[str, Any] = {
        "run_id": run_id,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dry_run": bool(args.dry_run),
        "peer": {"host": peer.host, "user": peer.user, "hub": isinstance(peer, Hub),
                 "local": peer.local},
        "profiles": {},
        "errors": [],
        "warnings": [],
    }
    log(f"sync run {run_id}{' (DRY RUN)' if args.dry_run else ''} -> {peer.label}")
    progress = ProgressWriter(run_dir, run_id, bool(args.dry_run), [p.name for p in selected])

    rc = 0
    try:
        ensure_peer_engine(peer)
    except HomesyncError as e:
        progress.error(str(e))
        progress.finish("error")
        print(f"error: {e}", file=sys.stderr)
        return 1

    auto_apply_later: list[Profile] = []
    via_hub = isinstance(peer, Hub)
    for prof in selected:
        try:
            if prof.kind == "git":
                # A git profile snapshots ONLY on the producing side (push/both).
                # A pull-only box is a receiver: snapshotting there would create
                # competing bundles that could beat the producer's under
                # newest-wins and make apply a no-op. Bundles arrive via the
                # files-profile that carries the bundle dir; restore with
                # `fsync git apply` (or auto-apply, guarded, when apply:auto).
                # Under a hub a `both` profile does BOTH: snapshot its own repos
                # now, then (post-pass) apply the newest snapshot of another box.
                if git_profile_snapshots(prof):
                    applies = auto_apply_git_repos(prof, via_hub=via_hub)
                    try:
                        report["profiles"][prof.name] = snapshot_git_profile(
                            prof, DEFAULT_GIT_BUNDLE_DIR, log,
                            hub=peer if isinstance(peer, Hub) else None, control=control)
                    except (HomesyncError, grs.GitSyncError) as e:
                        if not applies:
                            raise
                        # `both`: a failed snapshot of our own repos must not
                        # stop us from applying the other boxes' newest ones.
                        rc = _note_kind_error(report, prof, e, log)
                    if applies:
                        auto_apply_later.append(prof)
                elif auto_apply_git_repos(prof, via_hub=via_hub):
                    # Defer to a post-pass so the git-bundles file profile (which
                    # carries fresh bundles) has already run this cycle.
                    auto_apply_later.append(prof)
                    report["profiles"][prof.name] = {"kind": "git", "apply": "auto",
                                                     "pending": True}
                    log(f"  {prof.name}: pull-only receiver, apply:auto — "
                        f"auto-apply after bundle sync")
                else:
                    log(f"  {prof.name}: pull-only (receiver) — not snapshotting; "
                        f"bundles arrive via file-sync, apply with `fsync git apply`")
                    report["profiles"][prof.name] = {"kind": "git", "snapshotted": False,
                                                     "reason": "pull-only receiver"}
            elif prof.kind == "svn":
                if svn_profile_snapshots(prof):
                    applies = auto_apply_svn_checkouts(prof, via_hub=via_hub)
                    try:
                        report["profiles"][prof.name] = snapshot_svn_profile(
                            prof, DEFAULT_SVN_SNAPSHOT_DIR, log,
                            hub=peer if isinstance(peer, Hub) else None, control=control)
                    except (HomesyncError, svs.SvnSyncError) as e:
                        if not applies:
                            raise
                        rc = _note_kind_error(report, prof, e, log)
                    if applies:
                        auto_apply_later.append(prof)
                elif auto_apply_svn_checkouts(prof, via_hub=via_hub):
                    auto_apply_later.append(prof)
                    report["profiles"][prof.name] = {"kind": "svn", "apply": "auto",
                                                     "pending": True}
                    log(f"  {prof.name}: pull-only receiver, apply:auto — "
                        f"auto-apply after snapshot sync")
                else:
                    log(f"  {prof.name}: pull-only (receiver) — not snapshotting; "
                        f"snapshots arrive via file-sync, apply with `fsync svn apply`")
                    report["profiles"][prof.name] = {"kind": "svn", "snapshotted": False,
                                                     "reason": "pull-only receiver"}
            else:
                res = _run_file_profile(
                    control, peer, prof, run_dir=run_dir, run_id=run_id,
                    dry_run=args.dry_run, workers=args.workers, log=log, progress=progress,
                    allow_mass_delete=bool(getattr(args, "allow_mass_delete", False)),
                )
                report["profiles"][prof.name] = res
                # ISSUE-010: a file profile's warnings (stale svn sender, resurrections,
                # a dead parity probe) reach the desktop like auto-apply's do.
                report["warnings"].extend(res.get("warnings") or [])
            progress.profile_done(prof.name)
        except (HomesyncError, grs.GitSyncError, svs.SvnSyncError) as e:
            report["profiles"].setdefault(prof.name, {})["status"] = "error"
            report["errors"].append(f"{prof.name}: {e}")
            progress.error(f"{prof.name}: {e}")
            progress.profile_done(prof.name, status="error")
            log(f"  {prof.name}: ERROR {e}")
            rc = 1
        except Exception as e:  # noqa: BLE001 — ISSUE-009: one profile must never take the run down
            # Anything unexpected (a raw subprocess error, a bug) is confined to
            # this profile: recorded with its traceback, and the run goes on so
            # the report is still written and the FAILED notification fires.
            msg = f"{prof.name}: unexpected {type(e).__name__}: {e}"
            report["profiles"].setdefault(prof.name, {})["status"] = "error"
            report["errors"].append(msg)
            progress.error(msg)
            progress.profile_done(prof.name, status="error")
            log(f"  {prof.name}: ERROR {msg}")
            log(traceback.format_exc().rstrip())
            rc = 1

    # Fix C post-pass: guarded receiver auto-apply, now that fresh bundles have
    # arrived via the git-bundles file profile this cycle.
    for prof in auto_apply_later:
        try:
            _hub = peer if isinstance(peer, Hub) else None
            if prof.kind == "svn":
                res = auto_apply_svn_profile(prof, DEFAULT_SVN_SNAPSHOT_DIR,
                                             run_id=run_id, dry_run=args.dry_run, log=log,
                                             hub=_hub, control=control)
            else:
                res = auto_apply_git_profile(prof, DEFAULT_GIT_BUNDLE_DIR,
                                             run_id=run_id, dry_run=args.dry_run, log=log,
                                             hub=_hub, control=control)
            prev = report["profiles"].get(prof.name) or {}
            if not prev.get("pending"):
                # `both` under a hub: this run's own snapshot result (repos /
                # checkouts) stays at the top level, the apply result joins it;
                # a failed snapshot keeps the profile at status error.
                res = {**prev, **res, "snapshot": prev}
                if prev.get("status") == "error":
                    res["status"] = "error"
            report["profiles"][prof.name] = res
            log(f"  {prof.name}: auto-apply — {len(res['applied'])} applied, "
                f"{len(res['skipped'])} skipped, {len(res['errors'])} error(s)"
                + (f", {len(res['sticky'])} stuck ≥{AUTO_APPLY_STICKY_RUNS} runs"
                   if res.get("sticky") else ""))
            report["warnings"].extend(res.get("warnings") or [])
            if res["errors"]:
                report["errors"].extend(f"{prof.name}: {e}" for e in res["errors"])
                rc = 1
        except (HomesyncError, grs.GitSyncError, svs.SvnSyncError) as e:
            report["profiles"][prof.name] = {"kind": prof.kind, "apply": "auto", "status": "error"}
            report["errors"].append(f"{prof.name}: auto-apply: {e}")
            log(f"  {prof.name}: auto-apply ERROR {e}")
            rc = 1
        except Exception as e:  # noqa: BLE001 — ISSUE-009, as above
            msg = f"{prof.name}: auto-apply: unexpected {type(e).__name__}: {e}"
            report["profiles"][prof.name] = {"kind": prof.kind, "apply": "auto", "status": "error"}
            report["errors"].append(msg)
            log(f"  {prof.name}: ERROR {msg}")
            log(traceback.format_exc().rstrip())
            rc = 1

    report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (run_dir / "report.json").write_text(json.dumps(report, indent=2))
    (st_root / "runs" / "latest").write_text(run_id)
    progress.finish("error" if rc else "done")

    moved, conflicts, _ = report_totals(report)
    log(f"report: {run_dir / 'report.json'}"
        + (f" — {conflicts} conflict(s) held for review" if conflicts else ""))

    if getattr(args, "notify", False) and not args.dry_run:
        # Held conflicts are standing state (the claude profile always has
        # some) — only speak up when files moved or something went wrong.
        if rc != 0:
            _notify("fsync sync FAILED", "; ".join(report["errors"])[:200])
        elif moved or report["warnings"]:
            parts = ([f"{moved} file(s) synced with {peer.host}"
                      + (f", {conflicts} conflict(s) held" if conflicts else "")]
                     if moved else []) + report["warnings"]
            _notify("fsync sync" if moved else "fsync sync: attention", "; ".join(parts)[:200])
    return rc


def _plan_full_path(control, peer: Peer, prof: Profile, rel: str, key: str, n_workers: int,
                    *, allow_mass_delete: bool = False):
    """Index locally, fetch the hub view (journal since the base, else full
    state), classify (R32). Shared by the executing run and the preview."""
    home = Path.home()
    local_root = home if rel == "." else home / rel
    remote_root = remote_root_for(peer, prof, rel)
    idx = local_index(local_root, recursive=prof.recursive, exclude=prof.exclude, workers=n_workers)
    L = {it["path"]: it for it in idx if it.get("path")}
    base = tw.load_base(tw.base_path(prof.name, key))
    live = tomb = None
    head = 0
    if base is not None:
        j = control.journal(prof.name, base["seq"], prefix=rel)
        if not j.get("indexed", True):
            raise HomesyncError(f"hub profile '{prof.name}' is not indexed — run `fsync hub index` on the hub")
        if j.get("complete"):
            live, tomb = tw.hub_view_from_base(base)
            tw.apply_journal(live, tomb, j.get("entries") or [], strip_prefix=rel)
            head = int(j.get("seq") or 0)
    if live is None:
        st = control.state(prof.name, prefix=rel)
        if not st.get("indexed", True):
            raise HomesyncError(f"hub profile '{prof.name}' is not indexed — run `fsync hub index` on the hub")
        live, tomb = tw.strip_state_prefix(st, rel)
        head = int(st.get("seq") or 0)
    # Paths the profile EXCLUDES are not observed on this box — a tree newly
    # excluded (an svn checkout once `apply: auto` arrived, a new pattern) must
    # not read as "deleted locally" against a base that still lists it. Drop
    # them from every view; they leave the base this run.
    converged = tw.converged_base(base)
    if prof.exclude:
        ex = prof.exclude
        live = {p_: v for p_, v in live.items() if not matches_exclude(p_, ex)}
        tomb = {p_: v for p_, v in tomb.items() if not matches_exclude(p_, ex)}
        if converged is not None:
            converged = {p_: v for p_, v in converged.items() if not matches_exclude(p_, ex)}
    plan = tw.classify(converged, L, live, tomb,
                       conflict=prof.conflict, merge_jsonl=prof.merge_jsonl)
    root_missing = not local_root.is_dir()
    if root_missing:
        # an absent local root says nothing about deletes (new box, unmounted tree)
        plan.del_hub = []
    tw.apply_direction(plan, prof.direction)
    tw.guard_mass_delete(plan, len(converged) if converged else 0, allow=allow_mass_delete)
    return local_root, remote_root, L, live, tomb, base, head, plan, root_missing, converged


def _move_to_backup(src: Path, backup_root: Path, relp: str) -> None:
    dst = backup_root / relp
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:
        import shutil
        shutil.move(str(src), str(dst))


def _run_path_full(control, peer: Peer, prof: Profile, rel: str, key: str, n_workers: int, *,
                   run_dir: Path, run_id: str, dry_run: bool, log, progress, local_backup: str,
                   remote_backup: str, lease_id: str | None,
                   allow_mass_delete: bool) -> dict[str, Any]:
    """One profile path in mode: full — plan, two rsync legs, local removals to
    the backup dir, hub commit (puts verified there, dels → tombstones, holds),
    then the new base. Anything that did not converge keeps its old base entry."""
    local_root, remote_root, L, live, tomb, base, head, plan, root_missing, converged = _plan_full_path(
        control, peer, prof, rel, key, n_workers, allow_mass_delete=allow_mass_delete)
    warnings: list[str] = []
    host = _local_short_host()

    # both-changed merge_jsonl → union merge locally, then push (as in v1)
    merged: list[str] = []
    would_merge: list[str] = []
    merge_failed: list[str] = []
    if plan.merge and dry_run:
        would_merge = list(plan.merge)
        if prof.direction in ("both", "push"):
            plan.push.extend(plan.merge)
    elif plan.merge:
        if progress:
            progress.path_phase(prof.name, rel, "merging")
        for relp in plan.merge:
            try:
                proc = _ssh(peer, f"cat {shlex.quote(remote_root + '/' + relp)}")
                if proc.returncode != 0:
                    raise HomesyncError(proc.stderr.strip().splitlines()[-1]
                                        if proc.stderr.strip() else f"cat rc={proc.returncode}")
                local_file = local_root / relp
                union = union_merge_jsonl(local_file.read_text(), proc.stdout)
                bak = Path(local_backup) / relp
                bak.parent.mkdir(parents=True, exist_ok=True)
                bak.write_text(local_file.read_text())
                tmp = local_file.with_suffix(local_file.suffix + ".fsync-merge")
                tmp.write_text(union)
                os.replace(tmp, local_file)
                st = local_file.stat()
                L[relp] = {"path": relp, "size": st.st_size, "mtime": st.st_mtime, "hash": None}
                merged.append(relp)
                if prof.direction in ("both", "push"):
                    plan.push.append(relp)
                    plan.overwrite_push.append(relp)
            except (OSError, HomesyncError) as e:
                merge_failed.append(relp)
                log(f"  {prof.name}/{rel}: merge of {relp} failed ({e}) — left unresolved")

    if progress:
        progress.path_phase(prof.name, rel, "a_to_b", planned=len(plan.push))
    leg_ab = run_rsync_leg(
        plan.push, str(local_root), peer.spec(remote_root), remote_backup,
        run_dir / prof.name / f"{key}.a_to_b.lst", dry_run=dry_run, overwrites=len(plan.overwrite_push),
        on_progress=(lambda b, p_, fd, ft: progress.leg_progress(prof.name, rel, "a_to_b", b, p_, fd, ft))
        if progress else None)
    if progress:
        progress.path_phase(prof.name, rel, "b_to_a", planned=len(plan.pull))
    leg_ba = run_rsync_leg(
        plan.pull, peer.spec(remote_root), str(local_root), local_backup,
        run_dir / prof.name / f"{key}.b_to_a.lst", dry_run=dry_run, overwrites=len(plan.overwrite_pull),
        on_progress=(lambda b, p_, fd, ft: progress.leg_progress(prof.name, rel, "b_to_a", b, p_, fd, ft))
        if progress else None)

    # hub tombstones → local backup dir (never rm)
    deleted_local: list[str] = []
    del_local_failed: list[str] = []
    if plan.del_local and not dry_run:
        if progress:
            progress.path_phase(prof.name, rel, "deleting", planned=len(plan.del_local))
        for relp in plan.del_local:
            src = local_root / relp
            try:
                if src.is_file() or src.is_symlink():
                    _move_to_backup(src, Path(local_backup), relp)
                deleted_local.append(relp)
            except OSError as e:
                del_local_failed.append(relp)
                warnings.append(f"{prof.name}/{rel}: could not move {relp} to the backup dir: {e}")

    # verify pulls landed (size + mtime vs the hub view) before trusting them in the base
    pulled_ok: set[str] = set()
    pulled_failed: set[str] = set()
    if not dry_run:
        for relp in plan.pull:
            h = live.get(relp)
            try:
                lp = local_root / relp
                if lp.is_symlink():
                    # rsync -a carries a link as a link; its target's stats are
                    # per box, so size/mtime cannot be compared — presence is enough
                    ok = True
                else:
                    st = lp.stat()
                    ok = h is not None and st.st_size == (h[0] if h[0] is not None else st.st_size) \
                        and abs(st.st_mtime - float(h[1] or st.st_mtime)) <= tw.MTIME_TOLERANCE
            except OSError:
                ok = False
            (pulled_ok if ok else pulled_failed).add(relp)

    # commit to the hub
    commit: dict[str, Any] = {"seq_to": head, "accepted_puts": [], "accepted_dels": [], "rejected": []}
    if not dry_run:
        puts = [{"path": tw.hub_path(rel, p_), "size": L[p_].get("size"), "mtime": L[p_].get("mtime"),
                 "hash": L[p_].get("hash")} for p_ in plan.push if p_ in L]
        dels = [{"path": tw.hub_path(rel, p_), "hash": (base or {}).get("paths", {}).get(p_, [None, None, None])[2]
                 if base else None} for p_ in plan.del_hub]
        holds = [{**c, "path": tw.hub_path(rel, c["path"])} for c in plan.conflicts]
        if puts or dels or holds or base is None or head != (base or {}).get("seq"):
            commit = control.commit(prof.name, host, run_id, lease_id, puts, dels, holds)
    pre = rel.rstrip("/") + "/" if rel != "." else ""
    strip = lambda hp: hp[len(pre):] if pre and hp.startswith(pre) else hp  # noqa: E731
    accepted_puts = {strip(x) for x in commit.get("accepted_puts", [])}
    accepted_dels = {strip(x) for x in commit.get("accepted_dels", [])}
    rejected = [{"path": strip(r["path"]), "reason": r["reason"]} for r in commit.get("rejected", [])]
    rejected_puts = {r["path"] for r in rejected if r["path"] in set(plan.push)}
    rejected_dels = {r["path"] for r in rejected if r["path"] in set(plan.del_hub)}
    if pulled_failed:
        warnings.append(f"{prof.name}/{rel}: {len(pulled_failed)} pulled file(s) did not verify — "
                        f"re-pulled next run: " + ", ".join(sorted(pulled_failed)[:5]))
    if rejected:
        warnings.append(f"{prof.name}/{rel}: hub rejected {len(rejected)} change(s) — retried next run: "
                        + ", ".join(f"{r['path']} ({r['reason']})" for r in rejected[:5]))
    if plan.mass_delete_held:
        warnings.append(f"{prof.name}/{rel}: {len(plan.mass_delete_held)} local deletion(s) HELD by the "
                        f"mass-delete guard (≥{tw.MASS_DELETE_MIN} and ≥{int(tw.MASS_DELETE_FRACTION * 100)}% "
                        f"of the base) — re-run with --allow-mass-delete if intended")
    if plan.conflicts:
        warnings.append(f"{prof.name}/{rel}: {len(plan.conflicts)} conflict(s) HELD on the hub "
                        f"(conflict: review) — `fsync hub holds`")
    if root_missing and (base or {}).get("paths"):
        warnings.append(f"{prof.name}/{rel}: local root absent — no deletions derived this run")

    # new base = hub view as of seq_to (+ overrides for what did not converge)
    overrides: dict = {}
    if not dry_run:
        unresolved = (set(plan.stranded_push) | set(plan.stranded_pull) | set(plan.stranded_del_hub)
                      | set(plan.stranded_del_local) | set(plan.mass_delete_held) | set(merge_failed)
                      | set(del_local_failed) | {c["path"] for c in plan.conflicts})
        new_base, overrides = tw.next_base(
            live, L, converged,
            accepted_puts=accepted_puts, rejected_puts=rejected_puts,
            accepted_dels=accepted_dels, rejected_dels=rejected_dels,
            pulled_ok=pulled_ok, pulled_failed=pulled_failed,
            deleted_local=set(deleted_local), unresolved=unresolved)
        new_tomb = {p_: list(v) for p_, v in tomb.items() if p_ not in new_base}
        seq_to = int(commit.get("seq_to") or head)
        for p_ in accepted_dels:
            new_tomb[p_] = [time.time(), seq_to]
        tw.save_base(tw.base_path(prof.name, key), seq_to, new_base, run_id, tombstones=new_tomb,
                     overrides=overrides)

    stranded = (plan.stranded_push + plan.stranded_pull + plan.stranded_del_hub + plan.stranded_del_local)
    if plan.conflicts:
        cf = run_dir / prof.name / f"{key}.conflicts.json"
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(plan.conflicts, indent=2))
    if deleted_local or plan.del_hub or plan.mass_delete_held:
        lst = run_dir / prof.name / f"{key}.deletes.lst"
        lst.parent.mkdir(parents=True, exist_ok=True)
        lst.write_text("".join(f"local\t{p_}\n" for p_ in deleted_local)
                       + "".join(f"hub\t{p_}\n" for p_ in plan.del_hub)
                       + "".join(f"held\t{p_}\n" for p_ in plan.mass_delete_held))
    attention = bool(stranded or plan.conflicts or plan.mass_delete_held or rejected or pulled_failed
                     or merge_failed or del_local_failed)
    log(f"  {prof.name}/{rel}: push {len(plan.push)}"
        + (f" ({len(plan.overwrite_push)} overwrite->backup)" if plan.overwrite_push else "")
        + f", pull {len(plan.pull)}"
        + (f" ({len(plan.overwrite_pull)} overwrite->backup)" if plan.overwrite_pull else "")
        + (f", del→hub {len(plan.del_hub)}" if plan.del_hub else "")
        + (f", del←hub {len(deleted_local) if not dry_run else len(plan.del_local)}" if plan.del_local else "")
        + (f", held {len(plan.conflicts)}" if plan.conflicts else "")
        + (f", merged {len(merged)}" if merged else "")
        + (f", would merge {len(would_merge)}" if would_merge else "")
        + f", identical {plan.noop}"
        + (f", {prof.direction}-only ({len(stranded)} STRANDED)" if stranded else "")
        + (f", MASS-DELETE HELD {len(plan.mass_delete_held)}" if plan.mass_delete_held else "")
        + (f", rejected {len(rejected)}" if rejected else "")
        + f" [seq {commit.get('seq_to', head)}]")
    for w in warnings:
        log(f"  ! {w}")
    return {
        "mode": "full",
        "a_to_b": leg_ab, "b_to_a": leg_ba,
        "direction": prof.direction,
        "merged": merged, "would_merge": would_merge,
        "skipped_by_direction": len(stranded),
        "stranded": stranded[:STRANDED_SAMPLE],
        "stranded_truncated": len(stranded) > STRANDED_SAMPLE,
        "stranded_side": "hub" if prof.direction == "push" else "local",
        "stranded_only_on_side": len(plan.stranded_push) + len(plan.stranded_pull),
        "stranded_newer_on_side": 0,
        "conflicts": len(plan.conflicts),
        "renames_pending": 0, "renames_demoted": 0,
        "identical": plan.noop,
        "deleted_local": deleted_local[:STRANDED_SAMPLE] if not dry_run else plan.del_local[:STRANDED_SAMPLE],
        "deleted_local_count": len(deleted_local) if not dry_run else len(plan.del_local),
        "deleted_hub": plan.del_hub[:STRANDED_SAMPLE], "deleted_hub_count": len(plan.del_hub),
        "introductions": len(plan.introductions), "revived": plan.revived[:STRANDED_SAMPLE],
        "mass_delete_held": plan.mass_delete_held[:STRANDED_SAMPLE],
        "mass_delete_held_count": len(plan.mass_delete_held),
        "rejected": rejected[:STRANDED_SAMPLE], "pull_failed": sorted(pulled_failed)[:STRANDED_SAMPLE],
        "base_seq": (base or {}).get("seq"), "seq": commit.get("seq_to", head),
        "unconverged": len(overrides) if not dry_run else None,
        "first_run": base is None,
        "untracked_introductions": {"on_peer": [], "on_local": [], "count": 0, "truncated": False},
        "resurrected": {"on_peer": [], "on_local": [], "count": 0, "truncated": False, "list": None,
                        "baseline": {"peer": True, "local": True}},
        "attention": attention,
        "warnings": warnings,
    }


def _run_summary(res: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    """What the hub remembers about a spoke's run (R41): enough for
    `fsync hub status` to say which box moved what, and when."""
    up = down = conflicts = 0
    for pr in (res.get("paths") or {}).values():
        up += (pr.get("a_to_b") or {}).get("files_transferred", 0)
        down += (pr.get("b_to_a") or {}).get("files_transferred", 0)
        conflicts += pr.get("conflicts", 0)
    deleted = sum((pr.get("deleted_local_count") or 0) + (pr.get("deleted_hub_count") or 0)
                  for pr in (res.get("paths") or {}).values())
    return {"status": res.get("status"), "pushed": up, "pulled": down, "conflicts": conflicts,
            "deleted": deleted, "dry_run": dry_run, "fsync": FSYNC_VERSION}


def _run_file_profile(control, peer: Peer, prof: Profile, *, run_dir: Path, run_id: str,
                      dry_run: bool, workers: int | None, log, progress,
                      allow_mass_delete: bool = False) -> dict[str, Any]:
    """``run_profile`` under the hub's per-profile lease (P7.1, R36/R41); a
    plain pass-through for a v1 peer. A lease held elsewhere defers THIS
    profile (reported, exit 0 — the next run picks it up); the hub's control
    plane being unreachable is a profile error."""
    if control is None:
        return run_profile(peer, prof, run_dir=run_dir, run_id=run_id, dry_run=dry_run,
                           workers=workers, log=log, progress=progress)
    from .hubclient import HubApiError, HubUnavailable
    from .hubstate import LeaseHeld

    host = _local_short_host()
    try:
        with control.lease(prof.name, host, run_id) as lease:
            res = run_profile(peer, prof, run_dir=run_dir, run_id=run_id, dry_run=dry_run,
                              workers=workers, log=log, progress=progress, control=control,
                              lease_id=lease.lease_id, allow_mass_delete=allow_mass_delete)
    except LeaseHeld as e:
        log(f"  {prof.name}: {e} — deferring this profile")
        return {"conflict": prof.conflict, "paths": {}, "status": "deferred",
                "reason": str(e), "warnings": []}
    except (HubUnavailable, HubApiError) as e:
        raise HomesyncError(f"hub control plane: {e}") from e
    if lease.lost:
        msg = (f"{prof.name}: hub lease renewal failed mid-run ({lease.lost}) — another spoke "
               f"may have started on this profile; the next run re-converges")
        res.setdefault("warnings", []).append(msg)
        res["status"] = "attention"
        log(f"  ! {msg}")
    if not dry_run:
        try:
            control.record_run(host, prof.name, run_id, _run_summary(res, dry_run))
        except Exception as e:  # noqa: BLE001 — bookkeeping must never fail a finished transfer
            res.setdefault("warnings", []).append(
                f"{prof.name}: hub did not record this run ({type(e).__name__}: {e})")
    return res


# --------------------------------------------------------------------------- #
# systemd user timer (P3: hands-off runs; peer-away and peer-busy are          #
# already clean no-ops, so the service needs no gating of its own)             #
# --------------------------------------------------------------------------- #

UNIT_DIR = "~/.config/systemd/user"
UNIT_NAME = "fsync-sync"


def render_timer_units(python: str, interval: str) -> tuple[str, str]:
    """Render (service, timer) unit texts. Pure — unit-testable."""
    service = f"""[Unit]
Description=fsync home-sync run (all profiles)

[Service]
Type=oneshot
Nice=10
# notify-send inside a user service needs the session bus path
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
ExecStart={python} -m fsync.cli sync run --all --notify
"""
    timer = f"""[Unit]
Description=Periodic fsync home-sync (peer-away/peer-busy runs are clean no-ops)

[Timer]
OnBootSec=3min
# OnUnitInactiveSec, NOT OnUnitActiveSec: a Type=oneshot service may never
# latch the "active" state, which leaves OnUnitActiveSec with no reference
# point and silently stops rescheduling. Inactive-since is always defined:
# next fire = {interval} after the previous run finished.
OnUnitInactiveSec={interval}
RandomizedDelaySec=4min

[Install]
WantedBy=timers.target
"""
    return service, timer


def _systemctl(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *argv], capture_output=True, text=True)


def _cmd_timer(args) -> int:
    unit_dir = Path(UNIT_DIR).expanduser()
    svc_path = unit_dir / f"{UNIT_NAME}.service"
    tmr_path = unit_dir / f"{UNIT_NAME}.timer"

    if args.action == "install":
        unit_dir.mkdir(parents=True, exist_ok=True)
        service, timer = render_timer_units(sys.executable, args.interval)
        svc_path.write_text(service)
        tmr_path.write_text(timer)
        _systemctl("daemon-reload")
        en = _systemctl("enable", "--now", f"{UNIT_NAME}.timer")
        if en.returncode != 0:
            print(f"enable failed: {en.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"installed {tmr_path.name} (every {args.interval}, +boot; "
              f"randomized ≤4min to avoid colliding with the peer's timer)")
        out = _systemctl("list-timers", f"{UNIT_NAME}.timer", "--no-pager")
        print(out.stdout.strip())
        return 0

    if args.action == "remove":
        _systemctl("disable", "--now", f"{UNIT_NAME}.timer")
        removed = []
        for p in (svc_path, tmr_path):
            if p.exists():
                p.unlink()
                removed.append(p.name)
        _systemctl("daemon-reload")
        print(f"removed: {', '.join(removed) or 'nothing installed'}")
        return 0

    if args.action == "status":
        out = _systemctl("list-timers", f"{UNIT_NAME}.timer", "--no-pager")
        print(out.stdout.strip() or "timer not installed")
        latest = state_root() / "runs" / "latest"
        if latest.exists():
            run_id = latest.read_text().strip()
            rep_path = state_root() / "runs" / run_id / "report.json"
            if rep_path.exists():
                moved, _, errors = report_totals(json.loads(rep_path.read_text()))
                print(f"last run {run_id}: {moved} file(s) moved, {errors} error(s)")
        return 0

    print("usage: fsync sync timer {install|remove|status}", file=sys.stderr)
    return 2


def cmd_sync(args) -> int:
    if getattr(args, "sync_cmd", None) == "init":
        return _cmd_init(args)
    if getattr(args, "sync_cmd", None) == "run":
        return _cmd_run(args)
    if getattr(args, "sync_cmd", None) == "timer":
        return _cmd_timer(args)
    if getattr(args, "sync_cmd", None) == "folder":
        from .foldersync import cmd_sync_folder
        return cmd_sync_folder(args)
    print("usage: fsync sync {run|init|timer|folder} ...", file=sys.stderr)
    return 2
