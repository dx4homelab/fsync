# Git-repo sync — requirements & design draft (v1, 2026-07-09)

## Context

fsync's file profiles are **additive, newest-wins** replicators: correct for
Documents/Pictures/notes/`.claude`, deliberately delete-free (after the
deleted-file resurrection incident). That model **cannot** replicate a git
working tree, and trying to do so is what motivated this doc.

Observed 2026-07-09 on `dashboard-refactor-v4` (profile `primary`, one-way
fury4dx → minis4dx). `git status` on minis4dx showed 31 spurious entries:

- **7 phantom deletions** (` D`), all under `…/backups/` dirs. Those paths are
  git-**tracked**, but the `primary` profile has `exclude: [… "backups" …]`, so
  rsync never copies them while `.git` (which *is* synced) says they exist →
  git reports them deleted. *Any fsync exclude that matches a tracked path
  produces permanent phantom deletions on the receiver.*
- **24 untracked** (`??`) — working-tree files present with no matching index
  entry: the fingerprint of additive copy + an index at a different "version"
  than the worktree.

Three independent reasons file-sync ≠ git-state-sync:

1. **Additive, no delete propagation** — deletes/renames/checkouts on the
   producer linger on the receiver forever.
2. **Exclude ↔ tracked-path collision** — the 7 phantom deletions above.
3. **`.git` is a database, not a file tree** — index, refs, and loose/packed
   objects have mutual invariants and their own mtimes; per-file newest-wins can
   copy `.git` mid-write and land index, worktree, and object store at three
   different versions.

The correct transport for git state is git itself. But the reason file-sync was
reached for is real: `dashboard-refactor-v4`'s only remote is
`git.safety.af.mil` (AFSAS / .mil) — WIP must **not** be pushed there. So the
requirement is: *carry the exact local git working state between two personal
boxes, box-to-box, touching no external remote.*

## Requirements

- **R13 — Exact working-state fidelity.** After a sync, `git status` /
  `git status --porcelain` on the receiver is identical to the producer's at
  snapshot time: same checked-out branch/HEAD, same **staged** content, same
  **unstaged** tracked modifications (incl. deletions), same **untracked**
  (non-ignored) files. "Continue exactly where I left off," not just "same
  commits."
- **R14 — No external remote.** Transport is strictly box-to-box over the
  existing trusted channel. Nothing is pushed/fetched to `git.safety.af.mil` or
  any third party. Bundles are local artifacts.
- **R15 — Nothing on the receiver is ever lost.** Because direction is one-way
  and stateless (no divergence tracking, per the v1 scope decision), every apply
  **unconditionally** backs up the receiver's current working state first, into
  a recoverable artifact under `backup_root`. Clobbering is always reversible.
- **R16 — Safe transport over additive file-sync.** The on-wire artifact is a
  single, internally-consistent file per repo, carried by an ordinary fsync
  files-profile. No accumulation: a **stable filename per repo, overwritten**
  each run (timestamped names would accumulate forever under additive sync —
  the resurrection problem in bundle form).
- **R17 — Apply is a distinct, guarded step.** Snapshot (producer) is read-only
  and safe to run in the timer. Apply (receiver) mutates a working tree the user
  may be editing, so it is **explicit / on-demand** for v1, never silent in the
  hourly timer.
- **R18 — Auditable.** Each snapshot and apply leaves a machine-readable record
  (what repo, HEAD, dirty?, bundle bytes, backup path, outcome) in the run
  report, consistent with R7 of home-sync.
- **R19 — YAML config, no UI assumptions.** Configured via a new profile `kind`
  in `sync-profiles.yaml` (YAML only — JSON was rejected). Engine stays
  UI-agnostic; a TUI/web surface may come later.

### Non-goals (v1)

- **Divergence detection** ("both boxes advanced"). Stateless by decision; R15's
  unconditional backup is the safety net instead. Revisited in Roadmap.
- **Bidirectional git-sync.** One-way fury4dx → minis4dx, matching current file
  profiles.
- Stashes, reflog, per-repo hooks/config, **git-ignored** files (build output),
  submodule working states, git-LFS. All listed in Roadmap.

## Layered architecture

| Layer | Job | v1 choice |
|---|---|---|
| **Transport** | move objects+refs+WIP box-to-box | `git bundle` (single file), carried by a files-profile |
| **Packaging** | how git handling plugs into fsync | profile `kind: git` → `GitRepoSyncer` strategy |
| **State/coord** | track synced refs, detect divergence | **none in v1** (stateless); per-bundle meta only |

The `GitRepoSyncer` is a strategy behind a small seam, not a full plugin system
(two boxes, one backend). It graduates to a real plugin the day a third backend
(restic/rclone) appears.

## Fidelity capture (producer, fury4dx)

A bare `git bundle --all` carries only committed history. To meet R13 we snapshot
the **three trees** that define a working state — HEAD, index, full worktree —
using git plumbing (the same primitive `git stash create` is built on), without
disturbing the user's repo:

```
H=$(git rev-parse HEAD)                       # real HEAD commit
BR=$(git symbolic-ref -q --short HEAD || echo "DETACHED")

# (1) index → tree → commit
Tindex=$(git write-tree)
Cindex=$(git commit-tree "$Tindex" -p "$H" -m "fsync:index")

# (2) full worktree incl untracked(non-ignored) → tree → commit
export GIT_INDEX_FILE=$(mktemp -u)            # scratch index, never touch the real one
git read-tree "$H"
git add -A                                    # tracked mods + deletions + untracked
Twork=$(git write-tree)
rm -f "$GIT_INDEX_FILE"; unset GIT_INDEX_FILE
Cwork=$(git commit-tree "$Twork" -p "$H" -p "$Cindex" -m "fsync:wip")

git update-ref refs/fsync/wip "$Cwork"        # a ref so bundle can include the tip
git bundle create "<repo>.bundle" --all refs/fsync/wip
```

Capture is read-only w.r.t. the user's index/worktree/branches (it only writes
new objects + the `refs/fsync/wip` ref, which the next snapshot overwrites; GC
prunes the rest). It requires the repo **quiescent** (no `.git/index.lock`); if
locked, skip that repo this run and log it.

The bundle ships with a sidecar `**<repo>.meta.json**` (payload, not persistent
state): `{ branch|DETACHED, head, cindex, cwork, dirty, has_wip }`. Both files
live in `~/.fsync/git-bundles/`, a directory synced by an ordinary files-profile
(stable names, overwritten → newest-wins, no accumulation per R16).

## Fidelity apply (receiver, minis4dx) — always-backup

```
# (0) R15: unconditionally snapshot the receiver's OWN state first, recoverable
git bundle create "<backup_root>/<run>/<repo>.pre-apply.bundle" --all $(receiver refs/fsync/wip via same capture)
#     (+ record receiver branch/HEAD/dirty alongside)

# (1) bring producer objects/refs in without touching local refs yet
git fetch "<repo>.bundle" 'refs/heads/*:refs/fsync/incoming/heads/*' \
                          'refs/tags/*:refs/fsync/incoming/tags/*' \
                          'refs/fsync/wip:refs/fsync/incoming/wip'

# (2) reconstruct EXACT status from the three trees (H, Cindex, Cwork)
git update-ref refs/heads/$BR "$H"            # branch at producer's real HEAD
git symbolic-ref HEAD refs/heads/$BR
git read-tree --reset -u "$Cwork^{tree}"      # worktree + index = full worktree
git read-tree            "$Cindex^{tree}"     # index = staged snapshot (worktree untouched)
git update-index -q --refresh || true
```

Why this reproduces status exactly:

- worktree = `Cwork` tree (all tracked mods + untracked files materialized);
- index = `Cindex` tree (producer's staged state);
- HEAD = producer's real commit `H`.

So: file in index ≠ HEAD → **staged**; worktree ≠ index → **unstaged**; in
worktree but neither index nor HEAD → **untracked**; missing from worktree but
in index → **unstaged deletion**; missing from both but in HEAD → **staged
deletion**. That is byte-for-byte the producer's `git status --porcelain`.

Clean-repo fast path: `has_wip=false` ⇒ just move the branch to `H` and
`reset --hard` (still after the R15 backup).

## Edge cases

| Case | Handling |
|---|---|
| Clean worktree | fast path (branch move + hard reset) |
| Detached HEAD on producer | meta records `DETACHED`; apply checks out `H` detached |
| Unstaged / staged deletions | captured by temp-index `git add -A`; reconstructed by the two read-trees |
| Renames | carried as content (del+add); status may show rename — cosmetic |
| New branch on producer | `--all` carries it; created on receiver |
| Branch deleted on producer | v1: **kept** on receiver (additive); mirror-prune is a config flag (safe because R15 backs up) — default off, flagged in report |
| Git-ignored files | not carried (build output); R13 scope is non-ignored only |
| Submodules / LFS | v1 non-goal; detect + warn, don't silently half-sync |
| `index.lock` present (repo busy) | skip repo, log; snapshot is best-effort per run |
| Large full bundle (dashboard `.git` = 22 MB) | fine on 2.5G LAN; incremental is a Roadmap optimization (needs a basis ref = state) |

## Config (YAML)

```yaml
profiles:
  primary-repos:
    kind: git                     # new; default remains files
    paths: [workspaces/primary]   # discover repos (dirs containing .git) under here
    exclude: [".venv*", "venv*", "node_modules"]   # repo discovery + untracked capture
    include_untracked: true       # R13
    mirror_branches: false        # false = additive refs; true = prune to producer's set
    apply: on-demand              # R17; "auto" reserved for a future guarded mode
  # the bundle staging dir travels as an ordinary files-profile:
  git-bundles:
    paths: [".fsync/git-bundles"] # stable names, overwritten, newest-wins
```

## CLI

- `fsync git snapshot [profile]` — producer: write `<repo>.bundle` + meta for
  every repo discovered in git-kind profiles.
- `fsync git apply [profile] [--repo NAME]` — receiver: R15 backup, then
  reconstruct. Explicit per R17.
- `fsync git status [profile]` — dry-run: per repo, local HEAD/dirty vs the
  incoming bundle's HEAD/dirty; shows what an apply would change and where the
  backup would go. No mutation.

**Run sequencing.** `fsync sync run` (producer side) does: (1) `git snapshot`
for git-kind profiles → writes bundles, **then** (2) the ordinary file-sync legs
carry `git-bundles/`. Apply stays out of the timed run (R17); the receiver runs
`fsync git apply` on demand (or `fsync git status` to preview first).

## Testing

Property/round-trip is the core guarantee: for a repo in state *S*,
`snapshot → bundle → apply` into a fresh clone yields `git status --porcelain`
== *S*, identical index (`git write-tree` equal), and identical worktree file
hashes. Matrix: clean · staged-only · unstaged-only · untracked-only · deletions
(staged/unstaged) · mixed · detached HEAD · new branch · multiple repos under one
profile. Safety: assert every apply writes a `pre-apply.bundle` that restores the
receiver's prior work; simulate a dirty receiver and verify recovery.

## Phased plan

- **P5.1 — capture/reconstruct core (library + tests).** ✅ SHIPPED.
  `fsync/git_repo_sync.py`: `snapshot_repo()`, `apply_repo()` (R15
  always-backup), `discover_repos()`, `preview_apply()`. R13 fidelity proven by
  the round-trip matrix in `tests/test_git_repo_sync.py`.
- **P5.2 — profile kind + discovery + CLI.** ✅ SHIPPED. `kind: git` in
  `load_config`, repo discovery under `paths`, `fsync git snapshot|apply|status`;
  snapshot wired into `sync run` (producer path); designed to be carried by a
  `git-bundles/` files-profile. CLI integration + end-to-end smoke green.
- **P5.3 — safe-apply hardening + adversarial review.** TODO next. `index.lock`/
  busy guard, backup retention/GC, submodule/LFS detect-and-warn, an adversarial
  review pass (the P4 pattern), and enabling it on the live config.
- **P5.4 — (Roadmap, out of v1 scope).** Incremental bundles (`<base>..HEAD`,
  needs a basis ref) + a state layer (sqlite sidecar → central Postgres control
  plane per `fsync-control-plane`) enabling **divergence detection** and thus
  optional bidirectional git-sync. This is where ideas (a) Postgres and
  incremental transport return, once v1 proves the transport.

## Decisions taken (v1, implemented)

The three open questions were resolved as follows while building P5.1–P5.2:

1. **Branch-delete semantics** → **additive by default** (`mirror_branches:
   false`). Shared branches are always fast-forwarded to the producer's tips;
   receiver-only branches are kept unless `mirror_branches: true` (per-profile)
   or `fsync git apply --mirror-branches` is used. R15 backup makes prune safe.
2. **Which profiles are `kind: git`** → the **capability** is implemented and
   tested, but no *live* profile was flipped — enabling it on the deployed
   `sync-profiles.yaml` (both boxes) is a deploy step (see below), not a code
   change. Recommended target: split `workspaces/primary` (and later
   `workspaces/homelab`) repos into a `kind: git` profile, which also
   permanently kills the phantom-deletion class.
3. **Untracked capture bound** → **`.gitignore` only** for v1 (`git add -A` in a
   throwaway index). Applying the profile `exclude` as an `add -A` pathspec is a
   P5.3 refinement. Excludes *are* honored for repo **discovery**.

Also decided in build: bundles use **stable per-repo filenames** (overwritten,
newest-wins) with **deterministic snapshot commits** (fixed identity+date) so an
unchanged working state hashes to the same objects and the bundle doesn't churn;
first-time apply into a **missing/empty repo** is supported (init-from-bundle, no
backup — nothing to lose); `sync run` **snapshots** git profiles (producer-side,
idempotent) but never auto-applies (R17).

## Enabling it (deploy step — not yet done on the live boxes)

Add to `~/.config/fsync/sync-profiles.yaml` on **both** boxes:

```yaml
profiles:
  # 1. capture primary's repos to bundles (replaces file-syncing their .git)
  primary-repos:
    kind: git
    paths: [workspaces/primary]
    exclude: ['.venv', 'venv', 'node_modules']
    # mirror_branches: false   # default
  # 2. carry the bundle dir between boxes (ordinary additive file-sync)
  git-bundles:
    paths: ['.fsync/git-bundles']
```
Then on the source box `fsync sync run --all` (snapshots + carries bundles), and
on the other box `fsync git status` / `fsync git apply`. Remove
`workspaces/primary` from the old file `primary` profile at the same time so the
two mechanisms don't both touch the repos.
