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
  fsync product fix: **PLANNED — not yet implemented.**
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

### Fix (fsync product change) — planned
- **A. File-sync untracked-introduction report (primary fix).** After a file profile transfers into
  a directory that is a git worktree, run `git status --porcelain` and warn in the run report about
  files that landed **untracked** on the receiver. Surfaces this ghost regardless of transport.
  Detection only — no behavior change.
- **B. `kind:git` apply branch-divergence guard.** Before `fsync git apply`, if the receiver's
  checked-out branch/HEAD differs from the producer's, warn (and require `--force`), because apply
  silently switches the receiver onto the producer's branch. Guards a real (separate) footgun.
- **C. (future, behavioral) File profiles skip git worktrees** owned by a `kind:git` profile, so a
  repo travels only by bundle. Needs an apply story (auto-apply or explicit step) so fury's repos
  don't go stale; design separately.

### Prevention
Keep a given repo on the **same branch** across both boxes. Do fsync/repo edits on the minis master.
A narrow `exclude *.whl` would only mask this one symptom — not a real fix.
