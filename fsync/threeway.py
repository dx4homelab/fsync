"""Three-way reconcile for the hub topology (P7.2, docs/hub-sync.md R32–R35).

Pure functions over three views of one profile path:

    base   B  what this spoke last converged to with the hub   {path: [size, mtime, hash]}
    local  L  this box's tree now                              {path: {size, mtime, hash}}
    hub    H  the hub's paths table now                        live {path: [size, mtime, hash, seq]}
                                                               tombstones {path: [deleted_at, seq]}

``classify`` turns them into actions; nothing here touches a file. The base
file lives at ``~/.fsync/base/<profile>/<key>.json.gz`` and is only rewritten
after a run committed (so a failed run re-derives the same plan next time).
"""

from __future__ import annotations

import gzip
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MTIME_TOLERANCE = 1.0
# mass-delete guard: more local deletions than this (and than this fraction of
# the base) in one run are held back — an unmounted or mis-excluded tree must
# never become a fleet-wide tombstone wave. Reported, applied only on --allow-mass-delete.
MASS_DELETE_MIN = 500
MASS_DELETE_FRACTION = 0.2


# --------------------------------------------------------------------------- #
# base file                                                                   #
# --------------------------------------------------------------------------- #

def base_path(prof_name: str, key: str, home: Path | None = None) -> Path:
    return (home or Path.home()) / ".fsync" / "base" / prof_name / f"{key}.json.gz"


def load_base(path: Path) -> dict[str, Any] | None:
    """{"seq": int, "paths": {p: [size, mtime, hash]}} or None (no baseline yet)."""
    try:
        with gzip.open(path, "rt") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("paths"), dict):
            return {"seq": int(data.get("seq") or 0), "paths": data["paths"],
                    "tombstones": data.get("tombstones") or {},
                    "overrides": data.get("overrides") or {},
                    "ts": data.get("ts"), "run_id": data.get("run_id")}
    except (OSError, ValueError):
        pass
    return None


def save_base(path: Path, seq: int, paths: dict[str, list], run_id: str,
              tombstones: dict[str, list] | None = None,
              overrides: dict[str, list | None] | None = None) -> None:
    """``paths``/``tombstones`` = the hub's exact view at ``seq`` (the journal is
    replayed on top of it next run). ``overrides`` = paths this run did NOT
    converge (held, stranded, rejected, failed): what this box last converged
    to for them — the entry, or None for "never had it" — so the classifier's
    B differs from the hub view exactly where it should."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump({"seq": int(seq), "ts": time.time(), "run_id": run_id, "paths": paths,
                   "tombstones": tombstones or {}, "overrides": overrides or {}}, f)
    os.replace(tmp, path)


def converged_base(base: dict[str, Any] | None) -> dict[str, list] | None:
    """The classifier's B: the hub view at base.seq with the overrides applied."""
    if base is None:
        return None
    out = dict(base["paths"])
    for p, e in (base.get("overrides") or {}).items():
        if e is None:
            out.pop(p, None)
        else:
            out[p] = e
    return out


# --------------------------------------------------------------------------- #
# hub view maintenance                                                        #
# --------------------------------------------------------------------------- #

def hub_view_from_base(base: dict[str, Any] | None) -> tuple[dict[str, list], dict[str, list]]:
    """Start a hub view from the base (what the hub had at base.seq) — the
    journal since then is applied on top with ``apply_journal``."""
    if not base:
        return {}, {}
    live = {p: [v[0], v[1], v[2], base.get("seq", 0)] for p, v in base["paths"].items()}
    tomb = {p: list(v) for p, v in (base.get("tombstones") or {}).items()}
    return live, tomb


def apply_journal(live: dict[str, list], tomb: dict[str, list], entries: list[list],
                  strip_prefix: str = "") -> None:
    """Replay journal rows ``[seq, path, op, hash, size, mtime, origin, ts]`` onto
    (live, tomb) in place. ``strip_prefix`` maps hub paths (``<rel>/x``) to
    run-path-relative ones (``x``)."""
    pre = strip_prefix.rstrip("/") + "/" if strip_prefix and strip_prefix != "." else ""
    for seq, path, op, hash_, size, mtime, _origin, ts in entries:
        if pre:
            if not path.startswith(pre):
                continue
            path = path[len(pre):]
        if op == "put":
            live[path] = [size, mtime, hash_, seq]
            tomb.pop(path, None)
        elif op == "del":
            live.pop(path, None)
            tomb[path] = [ts, seq]
        # "hold" changes nothing in the view


def strip_state_prefix(state: dict[str, Any], rel: str) -> tuple[dict[str, list], dict[str, list]]:
    pre = rel.rstrip("/") + "/" if rel and rel != "." else ""
    n = len(pre)
    live = {p[n:]: v for p, v in state.get("paths", {}).items() if p.startswith(pre)}
    tomb = {p[n:]: v for p, v in state.get("tombstones", {}).items() if p.startswith(pre)}
    return live, tomb


def hub_path(rel: str, path: str) -> str:
    return path if rel in ("", ".") else f"{rel.rstrip('/')}/{path}"


# --------------------------------------------------------------------------- #
# classification                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class Plan:
    push: list[str] = field(default_factory=list)        # local → hub (put)
    pull: list[str] = field(default_factory=list)        # hub → local
    del_hub: list[str] = field(default_factory=list)     # local delete → hub tombstone
    del_local: list[str] = field(default_factory=list)   # hub tombstone → local backup+remove
    conflicts: list[dict] = field(default_factory=list)  # held (conflict: review)
    merge: list[str] = field(default_factory=list)       # both-changed merge_jsonl → union
    overwrite_push: list[str] = field(default_factory=list)  # pushes that replace a hub copy
    overwrite_pull: list[str] = field(default_factory=list)  # pulls that replace a local copy
    introductions: list[str] = field(default_factory=list)   # no base, local only (R42 report)
    revived: list[str] = field(default_factory=list)     # local re-created after a hub tombstone
    noop: int = 0
    stranded_push: list[str] = field(default_factory=list)   # withheld by direction
    stranded_pull: list[str] = field(default_factory=list)
    stranded_del_hub: list[str] = field(default_factory=list)
    stranded_del_local: list[str] = field(default_factory=list)
    mass_delete_held: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {"push": len(self.push), "pull": len(self.pull), "del_hub": len(self.del_hub),
                "del_local": len(self.del_local), "conflicts": len(self.conflicts),
                "merge": len(self.merge), "noop": self.noop,
                "introductions": len(self.introductions), "revived": len(self.revived),
                "stranded": len(self.stranded_push) + len(self.stranded_pull)
                + len(self.stranded_del_hub) + len(self.stranded_del_local),
                "mass_delete_held": len(self.mass_delete_held)}


def _same(a_size, a_mtime, a_hash, b_size, b_mtime, b_hash) -> bool:
    if a_hash and b_hash:
        return a_hash == b_hash
    return a_size == b_size and abs(float(a_mtime or 0) - float(b_mtime or 0)) <= MTIME_TOLERANCE


def _same_lb(local: dict, b: list) -> bool:
    return _same(local.get("size"), local.get("mtime"), local.get("hash"), b[0], b[1], b[2])


def _same_hb(h: list, b: list) -> bool:
    return _same(h[0], h[1], h[2], b[0], b[1], b[2])


def _same_lh(local: dict, h: list) -> bool:
    return _same(local.get("size"), local.get("mtime"), local.get("hash"), h[0], h[1], h[2])


def _matches_any(path: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    from fnmatch import fnmatch
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch(path, pat) or fnmatch(name, pat) for pat in patterns)


def classify(base: dict[str, list] | None, local: dict[str, dict], hub_live: dict[str, list],
             hub_tomb: dict[str, list], *, conflict: str = "newer",
             merge_jsonl: list[str] | None = None) -> Plan:
    """The R32 table. ``base`` None = first run (introductions reported, never
    treated as deletes). ``conflict``: newer (mtime decides, loser → backup) or
    review (held on the hub). Both-changed paths matching ``merge_jsonl`` are
    union-merged by the caller instead of resolved."""
    B = base or {}
    first_run = base is None
    plan = Plan()
    for path in sorted(set(B) | set(local) | set(hub_live) | set(hub_tomb)):
        b = B.get(path)
        l = local.get(path)
        h = hub_live.get(path)
        t = hub_tomb.get(path)

        if l is None and h is None:
            # nothing on either side now (both deleted, or tombstone we already applied)
            plan.noop += 1
            continue

        if b is None:
            # no baseline for this path
            if l is not None and h is None:
                if t is not None and float(t[0]) >= float(l.get("mtime") or 0):
                    plan.del_local.append(path)           # the delete is newer than our copy
                else:
                    plan.push.append(path)
                    (plan.revived if t is not None else plan.introductions).append(path)
                continue
            if l is None and h is not None:
                plan.pull.append(path)
                continue
            # both present without a base
            if _same_lh(l, h):
                plan.noop += 1
            else:
                _resolve(plan, path, l, h, conflict, merge_jsonl)
            continue

        # with a baseline
        l_changed = l is None or not _same_lb(l, b)
        h_changed = h is None or not _same_hb(h, b)
        if not l_changed and not h_changed:
            plan.noop += 1
        elif not l_changed and h_changed:
            if h is None:
                # hub lost it since the base: tombstone → remove locally; no row at all → re-offer
                if t is not None:
                    plan.del_local.append(path)
                else:
                    plan.push.append(path)
            else:
                plan.pull.append(path)
                plan.overwrite_pull.append(path)
        elif l_changed and not h_changed:
            if l is None:
                plan.del_hub.append(path)
            else:
                plan.push.append(path)
                plan.overwrite_push.append(path)
        else:
            # both changed since the base
            if l is None:
                # we deleted; the hub moved on (edit beats delete) or tombstoned too
                if h is not None:
                    plan.pull.append(path)
                else:
                    plan.noop += 1
            elif h is None:
                # hub tombstoned while we edited: our edit survives only if newer than the delete
                if t is not None and float(t[0]) >= float(l.get("mtime") or 0):
                    plan.del_local.append(path)
                else:
                    plan.push.append(path)
                    plan.revived.append(path)
            elif _same_lh(l, h):
                plan.noop += 1
            else:
                _resolve(plan, path, l, h, conflict, merge_jsonl)
    return plan


def _resolve(plan: Plan, path: str, l: dict, h: list, conflict: str,
             merge_jsonl: list[str] | None) -> None:
    if _matches_any(path, merge_jsonl or []):
        plan.merge.append(path)
        return
    if conflict == "review":
        plan.conflicts.append({"path": path, "incoming_hash": l.get("hash"),
                               "incoming_mtime": l.get("mtime"), "hub_hash": h[2]})
        return
    if conflict == "b-wins":
        plan.pull.append(path); plan.overwrite_pull.append(path); return
    if conflict == "a-wins" or float(l.get("mtime") or 0) > float(h[1] or 0):
        plan.push.append(path); plan.overwrite_push.append(path)
    else:
        plan.pull.append(path); plan.overwrite_pull.append(path)


def apply_direction(plan: Plan, direction: str) -> Plan:
    """direction filters ROUTING (as in v1): push = local→hub only, pull = hub→local
    only. Withheld actions are kept as ``stranded_*`` so they never render green."""
    if direction == "push":
        plan.stranded_pull, plan.pull = plan.pull, []
        plan.stranded_del_local, plan.del_local = plan.del_local, []
        plan.overwrite_pull = []
    elif direction == "pull":
        plan.stranded_push, plan.push = plan.push, []
        plan.stranded_del_hub, plan.del_hub = plan.del_hub, []
        plan.overwrite_push = []
        plan.introductions = []
        plan.revived = []
    return plan


def guard_mass_delete(plan: Plan, base_size: int, *, allow: bool = False) -> Plan:
    """Hold back a suspicious wave of local deletions (see MASS_DELETE_*)."""
    n = len(plan.del_hub)
    if not allow and n >= MASS_DELETE_MIN and n >= MASS_DELETE_FRACTION * max(base_size, 1):
        plan.mass_delete_held, plan.del_hub = plan.del_hub, []
    return plan


def next_base(hub_live: dict[str, list], local: dict[str, dict], old_base: dict[str, list] | None,
              *, accepted_puts: set[str], rejected_puts: set[str], accepted_dels: set[str],
              rejected_dels: set[str], pulled_ok: set[str], pulled_failed: set[str],
              deleted_local: set[str], unresolved: set[str]) -> tuple[dict[str, list], dict[str, list | None]]:
    """(hub view as of seq_to, converged overrides). The view = the hub's live
    paths with this run's accepted puts/dels applied. Paths that did not
    converge (failed pull, rejected put/del, held conflict, stranded) get an
    override holding the OLD converged entry (None = never had it), so the
    next run re-derives the same action instead of mistaking stale local
    content for a fresh edit — while the view itself stays exact."""
    B = old_base or {}
    view: dict[str, list] = {p: [h[0], h[1], h[2]] for p, h in hub_live.items()}
    for path in accepted_puts:
        l = local.get(path)
        if l is not None:
            view[path] = [l.get("size"), l.get("mtime"), l.get("hash")]
    for path in accepted_dels:
        view.pop(path, None)
    overrides: dict[str, list | None] = {}
    for path in rejected_puts | rejected_dels | pulled_failed | unresolved:
        if path in B or path in view:
            overrides[path] = B.get(path)
    return view, overrides
