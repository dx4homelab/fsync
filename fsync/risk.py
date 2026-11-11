"""`fsync risk` — what this box would lose if its drive died right now.

Goal 1 of fsync is to minimise the risk of losing files. Sync only protects
what a profile carries; this report walks $HOME and says, per file, whether a
copy leaves this drive, and ranks what does not by how replaceable it looks.

Read-only: it never contacts the hub and never writes outside stdout. Classes:

- ``covered``     carried by a file profile, a git snapshot or an svn snapshot;
- ``replaceable`` not carried, but a cache / reinstallable / re-clonable thing;
- ``at_risk``     not carried and not obviously replaceable — the list to read.

"Changed since last sync" (covered files newer than the last run) is the
window a failure right now would cost; it is an mtime estimate, not a plan.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

# (category, patterns). A pattern is fnmatch against the path relative to
# $HOME (``*`` spans ``/``); one starting with ``*/`` also matches at the top.
REPLACEABLE: list[tuple[str, list[str]]] = [
    ("caches", [
        ".cache/*", "*/__pycache__/*", "*/.pytest_cache/*", "*/.mypy_cache/*", "*/.ruff_cache/*",
        ".npm/*", ".cargo/registry/*", ".m2/repository/*", "*/.gradle/*", "*/.dart_tool/*",
        ".var/app/*/cache/*", ".local/share/Trash/*", "*/Cache/*", "*/Code Cache/*",
        "*/GPUCache/*", "*/CachedData/*", "*/component_crx_cache/*", "*/Service Worker/CacheStorage/*",
        "*.pyc", "*.class",
        ".claude/projects/*/tool-results/*", ".claude/projects/*/subagents/*", ".claude/shell-snapshots/*",
        ".claude/statsig/*", ".claude/cache/*", ".claude/debug/*", ".claude/paste-cache/*",
        ".claude/file-history/*", ".claude/backups/*",
    ]),
    ("installed software (reinstall)", [
        "*/node_modules/*", "*/.venv*/*", "*/venv*/*", ".local/share/containers/*",
        ".local/share/flatpak/*", ".local/share/claude/*", ".config/Claude/claude-code/*",
        ".vscode/extensions/*", ".vscode-server/*", ".dotnet/*", ".local/lib/*", ".local/pipx/*",
        ".claude/plugins/*", "*/.flutter-sdk/*", ".local/opt/*", ".rustup/*", "go/pkg/*",
    ]),
    ("disk images (*.iso, *.qcow2, ...)", ["*.iso", "*.qcow2", "*.raw", "*.vhd", "*.vhdx", "*.vmdk", "*.img"]),
    ("fsync's own state and backups", [".fsync/*", ".local/state/fsync/*"]),
]


def _match(rel: str, pat: str) -> bool:
    return fnmatch(rel, pat) or (pat.startswith("*/") and fnmatch(rel, pat[2:]))


def replaceable_category(rel: str) -> str | None:
    for cat, pats in REPLACEABLE:
        for pat in pats:
            if _match(rel, pat):
                return cat
    return None


def _first_exclude(sub: str, patterns: list[str]) -> str | None:
    """Same semantics as ``fileindex.matches_exclude``, returning the pattern."""
    parts = sub.split("/")
    for pat in patterns:
        if "/" in pat:
            if fnmatch(sub, pat):
                return pat
        elif any(fnmatch(p, pat) for p in parts):
            return pat
    return None


def _owner(rel: str, table: dict[str, Any]) -> str | None:
    for r in table:
        if rel == r or rel.startswith(r + "/"):
            return r
    return None


def _git(repo: Path, *args: str, timeout: int = 120) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              timeout=timeout).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _repo_facts(repo: Path) -> dict[str, Any]:
    remotes = {ln.split()[1] for ln in _git(repo, "remote", "-v").splitlines() if len(ln.split()) > 1}
    return {
        "remotes": len(remotes),
        "unpushed": len(_git(repo, "log", "--branches", "--not", "--remotes", "--oneline").splitlines()),
        "dirty": len(_git(repo, "status", "--porcelain").splitlines()),
        "stash": len(_git(repo, "stash", "list").splitlines()),
    }


def _last_run_time(state_root: Path) -> float | None:
    try:
        run_id = (state_root / "runs" / "latest").read_text().strip()
        return time.mktime(time.strptime(run_id[:15], "%Y%m%d-%H%M%S"))
    except (OSError, ValueError):
        return None


def _group_key(rel: str) -> str:
    parts = rel.split("/")
    if len(parts) == 1:
        return "(files directly in ~)"
    depth = 3 if parts[0] in ("workspaces", ".local") else 2
    return "/".join(parts[:min(depth, len(parts) - 1)])


class _Agg:
    __slots__ = ("files", "bytes", "recent_files", "recent_bytes", "newest", "samples", "why")

    def __init__(self) -> None:
        self.files = self.bytes = self.recent_files = self.recent_bytes = 0
        self.newest = 0.0
        self.samples: list[tuple[float, str]] = []
        self.why: dict[str, int] = {}

    def add(self, rel: str, size: int, mtime: float, recent: bool, why: str = "") -> None:
        self.files += 1
        self.bytes += size
        if recent:
            self.recent_files += 1
            self.recent_bytes += size
        if mtime > self.newest:
            self.newest = mtime
        if why:
            self.why[why] = self.why.get(why, 0) + 1
        if len(self.samples) < 3 or mtime > self.samples[-1][0]:
            self.samples.append((mtime, rel))
            self.samples.sort(reverse=True)
            del self.samples[3:]

    def as_dict(self) -> dict[str, Any]:
        return {"files": self.files, "bytes": self.bytes, "recent_files": self.recent_files,
                "recent_bytes": self.recent_bytes, "newest": self.newest,
                "why": max(self.why, key=self.why.get) if self.why else "",
                "samples": [r for _, r in self.samples]}


def scan(home: Path, profiles: dict[str, Any], *, git_owned: dict[str, Any], svn_owned: dict[str, Any],
         extra_excludes: dict[str, list[str]], state_root: Path, recent_days: int = 30,
         now: float | None = None) -> dict[str, Any]:
    """Classify every file under ``home``. Pure apart from reading the tree and
    asking git about repos; the caller supplies what the profiles own."""
    now = time.time() if now is None else now
    recent_cut = now - recent_days * 86400
    last_run = _last_run_time(state_root)
    file_profs = [p for p in profiles.values() if getattr(p, "kind", "files") == "files"]

    ignored: set[str] = set()
    for rel in git_owned:
        out = _git(home / rel, "ls-files", "-o", "-i", "--exclude-standard", "-z")
        ignored.update(f"{rel}/{x}" for x in out.split("\0") if x)

    covered: dict[str, _Agg] = {}
    replaceable: dict[str, _Agg] = {}
    at_risk: dict[str, _Agg] = {}
    gitdirs: dict[str, _Agg] = {}            # .git of repos no snapshot carries
    pending = _Agg()
    other_repos: list[str] = []
    try:
        dev = home.stat().st_dev
    except OSError:
        dev = None

    def put(table: dict[str, _Agg], key: str, *a: Any) -> None:
        table.setdefault(key, _Agg()).add(*a)

    for dp, dns, fns in os.walk(home):
        rel_dir = os.path.relpath(dp, home)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        if (".git" in dns or ".git" in fns) and rel_dir and not _owner(rel_dir, git_owned) \
                and not replaceable_category(rel_dir + "/.git/x"):
            other_repos.append(rel_dir)
        keep = []
        for d in dns:
            full = os.path.join(dp, d)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if os.path.islink(full) or (dev is not None and st.st_dev != dev):
                continue
            keep.append(d)
        dns[:] = keep
        for fn in fns:
            full = os.path.join(dp, fn)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            size, mtime = st.st_size, st.st_mtime
            recent = mtime >= recent_cut
            how = None
            g = _owner(rel, git_owned)
            s = None if g else _owner(rel, svn_owned)
            if g:
                if rel in ignored:
                    cat = replaceable_category(rel)
                    if cat:
                        put(replaceable, cat, rel, size, mtime, recent)
                    else:
                        put(at_risk, g, rel, size, mtime, recent, "git-ignored, so the git snapshot skips it")
                    continue
                how = "git snapshots"
            elif s:
                if rel[len(s) + 1:].startswith(".svn/"):
                    put(replaceable, "svn metadata (re-checkout from the server)", rel, size, mtime, recent)
                    continue
                how = "svn snapshots + svn server"
            else:
                excluded = None
                for p in file_profs:
                    for base in p.paths:
                        if base in (".", ""):
                            sub = rel
                        elif rel.startswith(base + "/"):
                            sub = rel[len(base) + 1:]
                        else:
                            continue
                        if not getattr(p, "recursive", True) and "/" in sub:
                            continue
                        pat = _first_exclude(sub, list(p.exclude) + extra_excludes.get(p.name, []))
                        if pat is None:
                            how = f"profile {p.name}"
                            break
                        if excluded is None:
                            excluded = f"excluded by '{pat}' in profile {p.name}"
                    if how:
                        break
                if how is None:
                    parts = rel.split("/")
                    cat = replaceable_category(rel)
                    if cat:
                        put(replaceable, cat, rel, size, mtime, recent)
                    elif ".git" in parts[:-1]:
                        put(gitdirs, "/".join(parts[:parts.index(".git")]), rel, size, mtime, recent)
                    else:
                        put(at_risk, _group_key(rel), rel, size, mtime, recent,
                            excluded or "in no sync profile")
                    continue
            put(covered, how, rel, size, mtime, recent)
            if last_run is not None and mtime > last_run:
                pending.add(rel, size, mtime, recent)

    repos = []
    for rel in sorted(set(other_repos)):
        facts = _repo_facts(home / rel)
        agg = gitdirs.pop(rel, None)
        safe = facts["remotes"] > 0 and facts["unpushed"] == 0 and facts["stash"] == 0
        if agg is not None:
            target = replaceable if safe else at_risk
            key = "git history that is also on a remote (re-clone)" if safe else rel + "/.git"
            dst = target.setdefault(key, _Agg())
            dst.files += agg.files
            dst.bytes += agg.bytes
            dst.recent_files += agg.recent_files
            dst.recent_bytes += agg.recent_bytes
            dst.newest = max(dst.newest, agg.newest)
            if not safe:
                dst.samples = agg.samples
                dst.why = {"git history with commits or stashes that are on no remote and in no snapshot": 1}
        if not safe or facts["dirty"]:
            repos.append({"rel": rel, **facts, "git_dir_bytes": agg.bytes if agg else 0})
    for rel, agg in gitdirs.items():            # a stray .git dir no walk matched as a repo
        put(at_risk, rel + "/.git", rel + "/.git", agg.bytes, agg.newest, False, "git metadata in no snapshot")

    def total(table: dict[str, _Agg]) -> dict[str, int]:
        return {"files": sum(a.files for a in table.values()), "bytes": sum(a.bytes for a in table.values()),
                "recent_files": sum(a.recent_files for a in table.values())}

    return {
        "host": socket.gethostname().split(".")[0], "home": str(home), "generated": now,
        "recent_days": recent_days, "last_run": last_run,
        "totals": {"covered": total(covered), "replaceable": total(replaceable), "at_risk": total(at_risk)},
        "pending": pending.as_dict(),
        "covered": {k: v.as_dict() for k, v in covered.items()},
        "replaceable": {k: v.as_dict() for k, v in replaceable.items()},
        "at_risk": {k: v.as_dict() for k, v in at_risk.items()},
        "repos_without_snapshot": repos,
        "git_snapshot_repos": len(git_owned), "svn_checkouts": len(svn_owned),
    }


# --------------------------------------------------------------------------- #
# rendering                                                                   #
# --------------------------------------------------------------------------- #

def _h(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "never"
    d = max(0, now - ts)
    if d < 5400:
        return f"{d / 60:.0f} min ago"
    if d < 2 * 86400:
        return f"{d / 3600:.0f} h ago"
    return f"{d / 86400:.0f} days ago"


def at_risk_order(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Recently touched groups first — your own files before application
    state (hidden top-level folders churn constantly) — then the rest by size."""
    def key(kv):
        name, a = kv
        if not a["recent_files"]:
            return (2, -a["bytes"])
        return (1 if name.startswith(".") else 0, -a["newest"])
    return sorted(report["at_risk"].items(), key=key)


def render(report: dict[str, Any], *, top: int = 25, daemon_state: str | None = None) -> str:
    now = report["generated"]
    t = report["totals"]
    L: list[str] = []
    L.append(f"fsync risk — {report['host']} — if this drive died now ({report['home']})")
    L.append("")
    L.append(f"  {'safe: a copy is off this drive':38} {_h(t['covered']['bytes']):>10} {t['covered']['files']:>9,} files")
    p = report["pending"]
    L.append(f"  {'  of which changed since last sync':38} {_h(p['bytes']):>10} {p['files']:>9,} files"
             f"   (last sync {_ago(report['last_run'], now)})")
    L.append(f"  {'LOST, hard to replace':38} {_h(t['at_risk']['bytes']):>10} {t['at_risk']['files']:>9,} files")
    L.append(f"  {'lost, replaceable':38} {_h(t['replaceable']['bytes']):>10} {t['replaceable']['files']:>9,} files")
    if daemon_state and daemon_state not in ("active", "not-installed"):
        L.append("")
        L.append(f"  WARNING: the sync service is '{daemon_state}' — scheduled sync may not be running")
    if report["last_run"] and now - report["last_run"] > 6 * 3600:
        L.append("")
        L.append(f"  WARNING: the last sync run was {_ago(report['last_run'], now)}")

    order = at_risk_order(report)
    L.append("")
    L.append(f"LOST, hard to replace — touched in the last {report['recent_days']} days first")
    if not order:
        L.append("  nothing")
    for key, a in order[:top]:
        recent = f"{a['recent_files']:,} recent" if a["recent_files"] else "none recent"
        L.append(f"  {_h(a['bytes']):>10} {a['files']:>7,} files  {recent:>13}  {key}")
        L.append(f"  {'':10} {a['why']}; newest {_ago(a['newest'], now)}"
                 + (f": {a['samples'][0]}" if a["samples"] else ""))
    if len(order) > top:
        rest = order[top:]
        L.append(f"  … {len(rest)} more group(s), {_h(sum(a['bytes'] for _, a in rest))} (--top 0 shows all)")
        L.append("")
        L.append("Largest of those")
        for key, a in sorted(rest, key=lambda kv: -kv[1]["bytes"])[:8]:
            L.append(f"  {_h(a['bytes']):>10} {a['files']:>7,} files  {key}  ({a['why']}; newest {_ago(a['newest'], now)})")

    repos = report["repos_without_snapshot"]
    if repos:
        L.append("")
        L.append("Git repos no snapshot carries")
        for r in repos:
            bits = []
            if not r["remotes"]:
                bits.append("NO remote")
            if r["unpushed"]:
                bits.append(f"{r['unpushed']} unpushed commit(s)")
            if r["stash"]:
                bits.append(f"{r['stash']} stash(es)")
            if r["dirty"]:
                bits.append(f"{r['dirty']} uncommitted change(s)")
            L.append(f"  {r['rel']}: {', '.join(bits)}")

    L.append("")
    L.append("Lost, replaceable")
    for key, a in sorted(report["replaceable"].items(), key=lambda kv: -kv[1]["bytes"]):
        L.append(f"  {_h(a['bytes']):>10} {a['files']:>7,} files  {key}")
    L.append("")
    L.append("Safe, by how it travels")
    for key, a in sorted(report["covered"].items(), key=lambda kv: -kv[1]["bytes"]):
        L.append(f"  {_h(a['bytes']):>10} {a['files']:>7,} files  {key}")
    L.append("")
    L.append("Notes: 'safe' means a profile carries the file, not that the hub copy was verified;")
    L.append("'changed since last sync' is judged by modification time. Only this user's home is scanned.")
    return "\n".join(L)


def cmd_risk(args) -> int:
    from . import homesync as hs
    try:
        _peer, profiles, _defaults = hs.load_config(getattr(args, "config", None))
    except hs.HomesyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    home = hs._home()
    git_owned: dict[str, Any] = {}
    svn_owned: dict[str, Any] = {}
    for p in profiles.values():
        if p.kind == "git":
            git_owned.update({r["rel"]: p.name for r in hs._discover_git_repos(p)})
        elif p.kind == "svn":
            svn_owned.update({r["rel"]: p.name for r in hs._discover_svn_checkouts(p)})
    report = scan(home, profiles, git_owned=git_owned, svn_owned=svn_owned,
                  extra_excludes=hs.git_repo_excludes_for_file_profiles(profiles),
                  state_root=hs.state_root(), recent_days=getattr(args, "recent_days", 30))
    daemon_state = None
    try:
        if hs._systemctl("is-enabled", hs.DAEMON_UNIT).returncode == 0:
            daemon_state = hs._systemctl("is-active", hs.DAEMON_UNIT).stdout.strip() or "unknown"
    except OSError:
        pass
    report["daemon"] = daemon_state
    if getattr(args, "json", False):
        print(json.dumps(report, indent=1))
    else:
        print(render(report, top=getattr(args, "top", 25) or 10 ** 9, daemon_state=daemon_state))
    return 0
