"""Ad-hoc two-box sync of a single folder, VCS-aware (``fsync sync folder``).

Unlike profile-driven ``fsync sync run``, this is a manual, on-demand reconcile
of ONE explicit path between this box and its peer. It inspects the folder and
picks a strategy per version-control kind:

  * ``.svn`` working copy -> SVN-FIRST. The clean way to reconcile two working
    copies is through the repository, not by byte-copying ``.svn`` metadata (the
    SQLite ``wc.db`` + ``pristine/`` store) between boxes, which splits the WC
    brain. So this probes both sides + the server and emits a *tiered,
    non-destructive* recommendation, running each action only on ``[y/N]``:

      Tier 0  read-only probes (svn info/status)          — always, no prompt
      Tier 1  suggest + [y/N], backup-first, reversible    — svn update/cleanup,
              svn diff capture, svn patch (dry-run gated)   from local state only
      Tier 2  suggest, needs an explicit typed confirm      — svn commit (writes
              (never the quick y/N)                          to the shared server)
      Tier 3  named but never auto-run (lossy)              — revert w/o backup,
              resolve --accept mine/theirs, cleanup          --remove-unversioned

    The invariant: nothing in Tier 0/1 can lose committed OR uncommitted content
    (every mutation captures a recoverable copy first), delete an unversioned
    file, or write to the server.

  * ``.git`` working copy -> defer to the full-fidelity bundle path
    (``fsync git`` / the ``repos`` profile); file-copying a repo caused the
    phantom-deletion incidents this tool exists to avoid.

  * plain folder -> the existing newest-wins ``run_profile`` engine (backup-dir
    safety, mTLS/ssh transport), as a one-off ad-hoc profile.
"""

from __future__ import annotations

import argparse
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import homesync as hs
from .homesync import HomesyncError, Peer


def _hostname() -> str:
    return socket.gethostname().split(".")[0]

# A runner executes a shell command inside `cwd` on some box and returns the
# CompletedProcess. Local and peer(ssh) runners share this shape so probing and
# execution are identical code paths — and trivially mockable in tests.
Runner = Callable[[str, str], subprocess.CompletedProcess]

TIER_READONLY = 0   # observation only
TIER_SAFE = 1       # mutating but reversible from local state; gated by [y/N]
TIER_CONFIRM = 2    # outward-facing / hard to reverse; needs a typed confirmation
TIER_HOLD = 3       # named as manual guidance, never executed by fsync

_PROBE = "@@FSYNC@@"  # section delimiter in the probe script's output


# --------------------------------------------------------------------------- #
# runners                                                                      #
# --------------------------------------------------------------------------- #

def local_runner(cwd: str, command: str, *, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"cd {shlex.quote(cwd)} && ( {command} )"],
        capture_output=True, text=True, timeout=timeout,
    )


def peer_runner(peer: Peer) -> Runner:
    def _run(cwd: str, command: str, *, timeout: int = 900) -> subprocess.CompletedProcess:
        return hs._ssh(peer, f"cd {shlex.quote(cwd)} && ( {command} )", timeout=timeout)
    return _run


# --------------------------------------------------------------------------- #
# VCS detection                                                                #
# --------------------------------------------------------------------------- #

def detect_vcs(runner: Runner, path: str) -> str:
    """Return 'svn' | 'git' | 'plain' | 'missing' for `path` as seen by `runner`."""
    probe = (
        f'if [ ! -d {shlex.quote(path)} ]; then echo missing; '
        f'elif [ -d {shlex.quote(path)}/.svn ]; then echo svn; '
        f'elif [ -e {shlex.quote(path)}/.git ]; then echo git; '
        f'else echo plain; fi'
    )
    proc = runner(".", probe, timeout=30)
    out = (proc.stdout or "").strip()
    return out if out in ("svn", "git", "plain", "missing") else "missing"


# --------------------------------------------------------------------------- #
# SVN probing                                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class SvnState:
    label: str
    wc_rev: Optional[int] = None
    url: Optional[str] = None
    server_rev: Optional[int] = None
    server_reachable: bool = False
    locked: bool = False
    modified: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unversioned: list[str] = field(default_factory=list)

    @property
    def has_local_mods(self) -> bool:
        return bool(self.modified or self.added or self.deleted)

    @property
    def behind(self) -> bool:
        return (self.server_rev is not None and self.wc_rev is not None
                and self.wc_rev < self.server_rev)


def _parse_status(text: str) -> dict[str, list[str]]:
    """Classify `svn status` lines by their first column (and the tree-conflict
    column). Only content states are tracked; ignored ('I') is dropped."""
    buckets: dict[str, list[str]] = {k: [] for k in
                                     ("modified", "added", "deleted", "conflicted",
                                      "missing", "unversioned")}
    locked = False
    for line in text.splitlines():
        if not line.strip():
            continue
        if "E155004" in line or "run 'svn cleanup'" in line or "is already locked" in line:
            locked = True
            continue
        code = line[0]
        path = line[8:].strip() if len(line) > 8 else line[1:].strip()
        # A working-copy lock shows an 'L' in the 3rd column.
        if len(line) > 2 and line[2] == "L":
            locked = True
        if code == "C" or (len(line) > 6 and line[6] == "C"):
            buckets["conflicted"].append(path)
        elif code in ("M", "R") or (len(line) > 1 and line[1] == "M" and code == " "):
            buckets["modified"].append(path)
        elif code == "A":
            buckets["added"].append(path)
        elif code == "D":
            buckets["deleted"].append(path)
        elif code == "!":
            buckets["missing"].append(path)
        elif code == "?":
            buckets["unversioned"].append(path)
    buckets["_locked"] = locked  # type: ignore[assignment]
    return buckets


def _to_int(text: str) -> Optional[int]:
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


def probe_svn(runner: Runner, path: str, label: str, *, server_timeout: int = 12) -> SvnState:
    """One round-trip: WC revision, URL, server HEAD (timed — unreachable is a
    normal answer, not an error), and local status."""
    url_cmd = "svn info --show-item url . 2>/dev/null"
    script = "\n".join([
        f'echo {_PROBE}rev;    svn info --show-item revision . 2>/dev/null',
        f'echo {_PROBE}url;    url=$({url_cmd}); printf "%s\\n" "$url"',
        f'echo {_PROBE}server; timeout {server_timeout} svn info --show-item revision "$url" 2>/dev/null || echo UNREACHABLE',
        f'echo {_PROBE}status; svn status 2>&1',
        f'echo {_PROBE}end',
    ])
    proc = runner(path, script, timeout=server_timeout + 60)
    sections: dict[str, list[str]] = {}
    cur = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith(_PROBE):
            cur = line[len(_PROBE):]
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(line)

    st = SvnState(label=label)
    st.wc_rev = _to_int("\n".join(sections.get("rev", [])))
    url = "\n".join(sections.get("url", [])).strip()
    st.url = url or None
    server_raw = "\n".join(sections.get("server", [])).strip()
    if server_raw and server_raw != "UNREACHABLE" and server_raw.isdigit():
        st.server_rev = int(server_raw)
        st.server_reachable = True
    buckets = _parse_status("\n".join(sections.get("status", [])))
    st.locked = bool(buckets.pop("_locked", False))
    for k, v in buckets.items():
        setattr(st, k, v)
    return st


# --------------------------------------------------------------------------- #
# recommendation (pure — no svn/ssh; fully unit-testable)                      #
# --------------------------------------------------------------------------- #

@dataclass
class Step:
    tier: int
    side: str            # "local" | "peer"
    cwd: str             # folder path on that side
    title: str
    commands: list[str]  # shell commands, run in order inside cwd
    rationale: str
    backup: Optional[str] = None   # recoverable copy this step writes first
    needs: Optional[str] = None    # a file that must exist for this step to run

    @property
    def runnable(self) -> bool:
        """Only Tier 1 is eligible for the quick [y/N] auto-approve path."""
        return self.tier == TIER_SAFE


def build_svn_plan(local: SvnState, peer: SvnState, *,
                   local_path: str, peer_path: str,
                   local_label: str, peer_label: str,
                   local_backup: str, peer_backup: str,
                   peer_target: str) -> list[Step]:
    """Turn two probed working copies into an ordered, tiered action list.

    Pure and deterministic: every branch here maps a detected condition to a
    step whose tier encodes its safety, so the executor never has to re-decide
    what is safe to run.
    """
    steps: list[Step] = []
    sides = [
        (local, "local", local_path, local_label, local_backup),
        (peer, "peer", peer_path, peer_label, peer_backup),
    ]

    for st, side, path, label, backup_dir in sides:
        patch = f"{backup_dir}/uncommitted.patch"

        if st.locked:
            steps.append(Step(
                TIER_SAFE, side, path,
                f"{label}: release the stale working-copy lock",
                ["svn cleanup"],
                "An interrupted operation left the WC locked; `svn cleanup` releases "
                "locks and temp state only — it touches no versioned or unversioned content.",
            ))

        if st.conflicted:
            steps.append(Step(
                TIER_HOLD, side, path,
                f"{label}: {len(st.conflicted)} unresolved conflict(s) — resolve manually",
                [f"# svn status  # then: svn resolve --accept working <path>  (your call)"],
                "fsync will not auto-pick a side. Resolve these yourself, then re-run.",
            ))
            # Don't stack an update on top of an already-conflicted WC.
            continue

        if st.has_local_mods:
            steps.append(Step(
                TIER_SAFE, side, path,
                f"{label}: back up uncommitted changes to a patch",
                [f"mkdir -p {shlex.quote(backup_dir)}",
                 f"svn diff > {shlex.quote(patch)}"],
                f"Captures a full recoverable copy of {label}'s local edits BEFORE any "
                "merge — nothing can be lost.",
                backup=patch,
            ))
            if st.behind:
                steps.append(Step(
                    TIER_SAFE, side, path,
                    f"{label}: update working copy r{st.wc_rev} -> r{st.server_rev} "
                    "(merges your edits; conflicts postponed)",
                    ["svn update --accept postpone"],
                    "Brings the WC current; your captured edits merge on top. Any conflict "
                    "becomes recoverable .mine/.rNNN markers, never a silent overwrite.",
                    backup=patch,
                ))
            steps.append(Step(
                TIER_CONFIRM, side, path,
                f"{label}: commit your changes to the SHARED repository",
                [f"svn commit -m {shlex.quote('WIP: reconcile via fsync')}"],
                "Outward-facing: publishes your edits to the team's server and is hard to "
                "reverse. Requires an explicit typed confirmation, never a quick y.",
            ))
        else:
            if st.behind:
                extra = " and restores missing items" if st.missing else ""
                steps.append(Step(
                    TIER_SAFE, side, path,
                    f"{label}: update working copy r{st.wc_rev} -> r{st.server_rev}",
                    ["svn update"],
                    f"No local modifications, so this is a clean fast-forward{extra}. "
                    "Nothing to discard.",
                ))
            elif st.missing:
                steps.append(Step(
                    TIER_SAFE, side, path,
                    f"{label}: restore {len(st.missing)} missing versioned item(s)",
                    ["svn update"],
                    "Missing items are re-fetched from the repo; nothing is discarded.",
                ))

    # Working-copy -> working-copy transfer of uncommitted work WITHOUT the
    # server. Only relevant when a side can't reach the server to commit/update;
    # when both are online, commit+update is the clean path so this stays hidden.
    # Emitted as MANUAL guidance rather than auto-run: moving a patch between two
    # boxes needs a route back (reverse ssh/scp) that fsync shouldn't assume, and
    # the exact `svn patch --dry-run` gate is safest done by hand.
    both_online = local.server_reachable and peer.server_reachable
    if not both_online:
        transfers = [(local, peer, peer_path, peer_label, local_backup),
                     (peer, local, local_path, local_label, peer_backup)]
        for src, dst, dst_path, dst_label, src_backup in transfers:
            # Needed when the side holding uncommitted work can't reach the
            # server to commit it; a patch is then the only way to move it.
            if src.has_local_mods and not src.server_reachable:
                src_patch = f"{src_backup}/uncommitted.patch"
                dst_side = "peer" if dst is peer else "local"
                steps.append(Step(
                    TIER_HOLD, dst_side, dst_path,
                    f"{dst_label}: (offline) apply {src.label}'s uncommitted change via patch",
                    [f"# 1. copy {src.label}:{src_patch} to this box",
                     f"# 2. svn patch --dry-run <patch>   # verify it applies",
                     f"# 3. svn patch <patch>"],
                    f"The server is unreachable from {dst_label}, so committing isn't "
                    f"possible. Move {src.label}'s captured patch over and apply it here "
                    "by hand (dry-run first; failures land in recoverable .rej files).",
                ))

    return steps


def unversioned_note(local: SvnState, peer: SvnState) -> Optional[str]:
    """SVN never carries '?' files; surface them so they aren't silently missed."""
    if not (local.unversioned or peer.unversioned):
        return None
    return (f"Unversioned files (outside SVN): {len(local.unversioned)} on {local.label}, "
            f"{len(peer.unversioned)} on {peer.label}. `svn update` won't move these. "
            f"Sync specific ones with a plain file copy if you need them on both boxes.")


# --------------------------------------------------------------------------- #
# rendering + execution                                                        #
# --------------------------------------------------------------------------- #

_TIER_LABEL = {
    TIER_READONLY: "read-only",
    TIER_SAFE: "safe [y/N]",
    TIER_CONFIRM: "confirm",
    TIER_HOLD: "manual",
}


def render_plan(steps: list[Step], note: Optional[str]) -> str:
    lines = []
    for i, s in enumerate(steps, 1):
        lines.append(f"[{i}] ({_TIER_LABEL[s.tier]}) {s.title}")
        lines.append(f"      why: {s.rationale}")
        if s.backup:
            lines.append(f"      backup: {s.backup}")
        for c in s.commands:
            lines.append(f"      $ {c}")
    if note:
        lines.append(f"\nnote: {note}")
    if not steps:
        lines.append("Both working copies are already in sync — nothing to do.")
    return "\n".join(lines)


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def execute_plan(steps: list[Step], runners: dict[str, Runner], *,
                 assume_yes: bool, ask=_ask, log=print) -> dict:
    """Run the plan honoring the tiers. Tier 1 -> [y/N] (or --yes). Tier 2 ->
    typed confirmation. Tier >=3 -> printed, never run."""
    report = {"applied": [], "skipped": [], "held": []}
    for i, s in enumerate(steps, 1):
        runner = runners[s.side]
        if s.tier >= TIER_HOLD:
            log(f"[{i}] MANUAL (not run): {s.title}")
            report["held"].append(s.title)
            continue
        if s.tier == TIER_CONFIRM:
            log(f"[{i}] {s.title}\n      {s.rationale}")
            token = "COMMIT"
            ans = ask(f"      This writes to the shared server. Type {token} to proceed (or Enter to skip): ")
            if ans.strip() != token:
                log("      skipped.")
                report["skipped"].append(s.title)
                continue
        else:  # TIER_SAFE
            if s.needs and not _remote_exists(runner, s.cwd, s.needs):
                log(f"[{i}] skipped (prerequisite {s.needs} not created): {s.title}")
                report["skipped"].append(s.title)
                continue
            if not assume_yes:
                ans = ask(f"[{i}] {s.title}\n      apply? [y/N]: ")
                if ans.strip().lower() not in ("y", "yes"):
                    report["skipped"].append(s.title)
                    continue
        ok = _run_step(runner, s, log)
        report["applied" if ok else "skipped"].append(s.title)
    return report


def _remote_exists(runner: Runner, cwd: str, path: str) -> bool:
    proc = runner(cwd, f"test -e {shlex.quote(path)}", timeout=30)
    return proc.returncode == 0


def _run_step(runner: Runner, s: Step, log) -> bool:
    for cmd in s.commands:
        if cmd.lstrip().startswith("#"):
            continue
        proc = runner(s.cwd, cmd, timeout=1800)
        out = (proc.stdout or "").strip()
        if out:
            log(out)
        if proc.returncode != 0:
            log(f"      ! command failed ({proc.returncode}): {cmd}\n{(proc.stderr or '').strip()}")
            return False
    return True


# --------------------------------------------------------------------------- #
# CLI entry                                                                    #
# --------------------------------------------------------------------------- #

def _resolve_paths(raw: str, peer_home: str) -> tuple[str, str, str]:
    """(local_abs, peer_abs, rel) — the folder must live under $HOME so the two
    boxes share the same home-relative layout (same assumption as profiles).

    A relative arg is interpreted home-relative (the canonical cross-box form,
    e.g. ``workspaces/primary/dashboard4trunk``), unless it actually resolves
    under the current directory — so running from inside the parent works too.
    """
    home = Path.home().resolve()
    p = Path(raw).expanduser()
    if p.is_absolute():
        local_abs = p.resolve()
    else:
        from_cwd = (Path.cwd() / p).resolve()
        from_home = (home / p).resolve()
        local_abs = from_cwd if from_cwd.exists() else from_home
    try:
        rel = local_abs.relative_to(home)
    except ValueError:
        raise HomesyncError(f"{local_abs} is not under your home {home} — "
                            "folder sync maps the same home-relative path on both boxes")
    peer_abs = f"{peer_home.rstrip('/')}/{rel}"
    return str(local_abs), peer_abs, str(rel)


def cmd_sync_folder(args: argparse.Namespace) -> int:
    def log(msg=""):
        print(msg, file=sys.stderr)

    try:
        peer, _profiles, _defaults = hs.load_config(getattr(args, "config", None))
    except HomesyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not hs.peer_reachable(peer):
        print(f"peer {peer.target} not reachable — nothing to do", file=sys.stderr)
        return 0

    try:
        p_home = hs.peer_home(peer)
        local_path, peer_path, rel = _resolve_paths(args.path, p_home)
    except HomesyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    lrun = local_runner
    prun = peer_runner(peer)

    local_kind = detect_vcs(lrun, local_path)
    peer_kind = detect_vcs(prun, peer_path)
    if local_kind == "missing":
        print(f"error: {local_path} does not exist here", file=sys.stderr)
        return 2

    log(f"folder: {rel}")
    log(f"  local ({_hostname()}): {local_kind}")
    log(f"  peer  ({peer.host}): {peer_kind}")

    if local_kind == "git" or peer_kind == "git":
        log("\nThis is a git working copy. Use the full-fidelity git path instead:")
        log("  fsync git snapshot   # producer side captures a bundle")
        log("  fsync git apply      # receiver restores exact working state")
        log("File-copying a git repo risks the phantom-deletion failures fsync avoids.")
        return 0

    if local_kind == "svn" and peer_kind == "svn":
        return _svn_reconcile(args, peer, local_path, peer_path, rel, lrun, prun, log)

    if local_kind == "svn" or peer_kind == "svn":
        print("error: one side is an SVN working copy and the other is not — "
              "check out the SVN working copy on both boxes first", file=sys.stderr)
        return 2

    # plain folders -> the existing newest-wins engine as a one-off profile.
    return _plain_reconcile(args, peer, rel, log)


def _svn_reconcile(args, peer: Peer, local_path, peer_path, rel, lrun, prun, log) -> int:
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-folder"
    safe = str(rel).replace("/", "_")
    local_backup = str((Path("~/.fsync/backups").expanduser() / run_id / safe / "local"))
    peer_backup = f"{hs.peer_home(peer)}/.fsync/backups/{run_id}/{safe}/peer"

    local = probe_svn(lrun, local_path, _hostname())
    peer_st = probe_svn(prun, peer_path, peer.host)

    log("\nSVN state:")
    for st in (local, peer_st):
        reach = f"server r{st.server_rev}" if st.server_reachable else "server UNREACHABLE"
        log(f"  {st.label}: WC r{st.wc_rev}  ({reach})  "
            f"mods={len(st.modified)+len(st.added)+len(st.deleted)} "
            f"missing={len(st.missing)} conflicts={len(st.conflicted)} "
            f"unversioned={len(st.unversioned)}"
            + ("  [LOCKED]" if st.locked else ""))

    steps = build_svn_plan(
        local, peer_st,
        local_path=local_path, peer_path=peer_path,
        local_label=local.label, peer_label=peer_st.label,
        local_backup=local_backup, peer_backup=peer_backup,
        peer_target=peer.target,
    )
    note = unversioned_note(local, peer_st)

    log("\nSVN-first recommendation:\n")
    log(render_plan(steps, note))

    if getattr(args, "dry_run", False) or not steps:
        return 0

    log("")
    report = execute_plan(steps, {"local": lrun, "peer": prun},
                          assume_yes=getattr(args, "yes", False), log=log)
    log(f"\napplied {len(report['applied'])}, skipped {len(report['skipped'])}, "
        f"held {len(report['held'])}")
    return 0


def _plain_reconcile(args, peer: Peer, rel, log) -> int:
    """Non-VCS folder: reuse run_profile's newest-wins engine + backup safety."""
    from dataclasses import replace as _replace  # noqa: F401

    direction = getattr(args, "direction", None) or "both"
    prof = hs.Profile(name=f"folder:{rel}", paths=[str(rel)], conflict="newer",
                      direction=direction, exclude=["__pycache__", "*.pyc", ".venv", "venv"])

    st_root = hs.state_root()
    st_root.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-folder"
    run_dir = st_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"plain folder — newest-wins sync (direction={direction})")
    if getattr(args, "dry_run", False):
        log("(dry-run)")
    try:
        hs.ensure_peer_engine(peer)
        result = hs.run_profile(peer, prof, run_dir=run_dir, run_id=run_id,
                                dry_run=getattr(args, "dry_run", False),
                                workers=None, log=log)
    except HomesyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    moved = sum(len(p.get("a_to_b", [])) + len(p.get("b_to_a", []))
                for p in result.get("paths", {}).values())
    log(f"done — {moved} file(s) {'would move' if getattr(args, 'dry_run', False) else 'moved'}")
    return 0
