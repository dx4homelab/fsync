# fsync — Issues & Fixes Log

A durable, git-tracked record of fsync issues, their **verified** diagnoses, and the fixes
(done or planned). Prefer this over ephemeral Claude handoff notes for anything cross-box:
it's versioned, and because the fsync repo's working files sync minis4dx → fury4dx via the
`homelab` file profile, this file is readable on fury too (fury has no `.git`, so read the
`.md`; git history lives on minis / origin).

**Workflow:** fsync development is authoritative on **minis4dx** (editable checkout + git origin);
fury4dx is a deployed receiver. Make changes on minis, commit + push, then
`fsync deploy push` + `fsync deploy daemon` to both boxes.

**Entry convention:** newest first. Each entry: `Status`, `Symptom`, `Root cause` (with code refs),
`Resolution` (for the incident), `Fix` (fsync product change), `Prevention`.

---

## ISSUE-001 — Cross-box "ghost": a file tracked on one branch reappears *untracked* on the peer

- **Status:** incident **RESOLVED** 2026-07-25 (both boxes aligned to `main @ 965a7e4`).
  fsync product fix: **A + B IMPLEMENTED in 0.4.0**; **C planned** (durable fix, under research).
- **Where seen:** repo `dashboard-refactor-v4` (under `workspaces/primary`), file
  `modules/vendor/dream4devops-0.8.0-py3-none-any.whl`.

### Symptom
`dream4devops-0.8.0-…whl` kept reappearing as an untracked (`??`) file on fury4dx after every
deletion — a "ghost" that would not stay gone.

### Root cause (verified against code, 2026-07-25)
1. The two boxes were on **different branches** of the same repo: minis4dx on `refactor-v4`
   (which *tracks* the 0.8.0 wheel), fury4dx on `main` (0.8.0 removed, 0.9.0 added).
2. The repo's **working files** are carried minis → fury by the **additive `primary` / `homelab`
   FILE profiles** — one-way, no-delete, and *branch-unaware*: they exclude only `.git`, not a
   repo's tracked files, and the file-sync engine (`run_profile`) has no git-awareness, so it
   copies a repo's working files as ordinary files.
3. On fury (`main`), the copied 0.8.0 lands **untracked**; deleting it is futile because the
   additive file sync **re-copies it every cycle** from minis's working tree.

**It was NOT the `kind:git` mirror** (contrary to an earlier fury handoff note that blamed it):
- The receiver never auto-applies bundles — a pull-only box logs
  *"not snapshotting; bundles arrive via file-sync, apply with `fsync git apply`"*
  ([`fsync/homesync.py`](../fsync/homesync.py) ~L1750). So `kind:git` puts nothing in fury's tree on its own.
- When you *do* apply, `_reconstruct` points HEAD at the **producer's** branch
  (`symbolic-ref HEAD refs/heads/{branch}`) and rebuilds the exact tree
  ([`fsync/git_repo_sync.py`](../fsync/git_repo_sync.py) ~L365) — so the file would be **tracked**, not an untracked ghost.
- The note's own detail — "re-mirrored next cycle" — only fits the hourly **file** sync, since
  `git apply` is manual. Hence the file profile is the carrier.

### Resolution (the incident)
Aligned minis to `main` (`git switch main` + fast-forward). Both boxes now on `main @ 965a7e4`,
0.8.0 gone both sides, 0.9.0 present, `git status` clean on both. Verified.

### Fix (fsync product change)
- **A. File-sync untracked-introduction report — DONE (0.4.0).** After a file leg transfers into a
  git worktree, a probe (`git_repo_sync.GHOST_PROBE`, run per receiving side via
  `homesync._scan_untracked_introductions`) reports transferred paths that git shows as `??`
  untracked on the receiver. Surfaced in the run report (`untracked_introductions`) and a `!` log
  warning. Detection only — no behavior change; best-effort (never fails a run).
- **B. `kind:git` apply branch-divergence guard — DONE (0.4.0).** `git_repo_sync.check_branch_divergence`
  compares the receiver's checked-out branch to the producer's; `fsync git apply` now SKIPS a
  divergent repo with a clear message unless `--force` is given (receiver is still backed up first).
- **C. (future, behavioral) File profiles skip git worktrees** owned by a `kind:git` profile, so a
  repo travels only by bundle. Needs an apply story (auto-apply or explicit step) so fury's repos
  don't go stale. **Under research** — see the research notes appended below when ready.

### Prevention
Keep a given repo on the **same branch** across both boxes. Do fsync/repo edits on the minis master.
A narrow `exclude *.whl` would only mask this one symptom — not a real fix.

### Durable fix (C) — research notes (2026-07-25, first pass)

**Scope of the double-coverage (measured).** All **26** repos owned by the `repos` git profile are
*also* carried by the file profiles (`homelab` 12 + `primary` 14). So every one can ghost under
branch divergence — this is systemic, not specific to dashboard-refactor-v4.

**Goal.** Stop double-covering: let each git-profile repo travel **only** by its bundle, and have the
file profiles skip it. That removes the ghost transport at the source.

**Hard constraint (found during research).** Do **not** skip *all* git worktrees in file sync: the
**fsync repo itself is deliberately excluded from the git profile** and relies on the `homelab` FILE
profile to reach fury (that is how this very doc gets there). C must skip only **git-profile-owned**
repos, and keep file-syncing the fsync repo, SVN working copies (e.g. `dashboard4trunk`), and plain dirs.

**Mechanism (feasible with existing machinery).** `fileindex.matches_exclude` already supports
path-form patterns (`repo/*` matches a whole subtree). So at run/config time: discover repos for
every `kind: git` profile in the config, and for each file profile inject an exclude for each such
repo's path **relative to that file profile's root** (e.g. `primary` → `dashboard-refactor-v4/*`,
`afsas/git4afsas/*`). Surgical; leaves fsync/SVN/plain untouched. Hook point: augment file
`Profile.exclude` in `load_config` (computed from *all* `kind: git` profiles, not just the ones
selected for a given run).

**The crux — the apply story.** With repos removed from file sync, fury updates them only via
`fsync git apply`, which is **manual today (R17)**. Options:
- **C-a — manual apply, staleness made visible.** File profiles skip repos; the sync report lists
  repos with unapplied bundles. Lowest risk; fury repos can lag until the user applies.
- **C-b — guarded auto-apply in the sync run (realizes R17's reserved "auto" mode).** After bundles
  arrive, auto-apply on the receiver **only when safe**: repo not busy (`repo_busy`), receiver clean
  (no local edits), and no branch divergence (Fix B). Otherwise skip + report. R15 backs up
  regardless. Fits the minis=master / fury=receiver model (fury repos are read-mostly).
- **C-c — separate scheduled apply** (daemon), same guards, decoupled from file sync.

**Recommendation (first pass): C-b + the exclude injection**, behind a per-git-profile config flag
`apply: on-demand | auto` (default `on-demand` to preserve v1 until proven). This removes the ghost
transport *and* keeps fury current, while Fix B's guard + R15 backup + clean/busy checks honor R17's
safety intent.

**Migration note.** Repo files already on fury (from prior additive file sync) stay put when excluded
(no-delete). The first guarded `apply` per repo reconciles the tree exactly (`read-tree --reset` +
`clean`), so strays are cleaned then — no separate cleanup needed.

**Open decisions (need user input before building):**
1. Auto-apply (C-b) or keep manual (C-a)? — the R17 v1→v2 call.
2. If auto-apply: confirm the safe policy = only when receiver repo is clean, not busy, same branch; else skip + report.
3. Config surface: per-`repos`-profile `apply:` flag, default `on-demand`.

**Next steps (not yet done):** prototype the exclude injection + a `sync run` report line for
skipped/unapplied repos; then, if C-b chosen, the guarded auto-apply path with tests.
