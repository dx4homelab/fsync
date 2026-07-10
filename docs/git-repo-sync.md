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
- Stashes, reflog, per-repo hooks/config, submodule working states, git-LFS
  object content. All listed in Roadmap.
- **Git-ignored files and stray nested repos on the receiver are left untouched**
  (`clean -fdq`, no `-x`/`-ff`) — never wiped, but also not made to match the
  producer, so `git status` may show a stray nested repo. R13's worktree
  identity is over *tracked + untracked-non-ignored* content; ignored build
  output is out of scope by design (you don't want `node_modules` nuked). Empty
  untracked directories don't survive (git can't track them).

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
- **P5.3 — safe-apply hardening + adversarial review.** LARGELY DONE (see
  outcomes below). Repo-busy guard, submodule/LFS warnings, adversarial
  edge-case tests, and the fixes from a 17-finding adversarial review. TODO:
  backup retention/GC, a `restore_backup()` inverse, and the live-config deploy.

### Adversarial review outcomes (P5.3)

A background adversarial review (git 2.55, all findings reproduced) surfaced 11
issues. Resolution:

**Fixed (data-loss / correctness):**
- **D1** — apply into a *populated non-git directory* used to skip the backup
  (`created=True`) and then `clean` wiped it. Now: only truly empty/missing dirs
  skip the backup; a populated non-git dir is tar'd to `backup_dir` first.
- **D3** — a receiver git-*ignored* file at a path the producer now *tracks* was
  overwritten and absent from the bundle backup (`add -A` skips ignored). Now:
  ignored collisions with the incoming tree are copied to
  `backup_dir/<name>.ignored/` before reconstruct.
- **F3** — tags were fetched but never recreated. Now reconstructed.
- **R1/F2** — `index.lock` / mid-merge-rebase repos: `repo_busy()` guard skips on
  snapshot, refuses on apply, at both the CLI and library layers.
- **R3** — pre-apply bundle now ships a `.pre-apply.meta.json` (receiver
  branch/HEAD/dirty) so it's interpretable for recovery.
- **R2** — a mid-reconstruct failure now raises pointing at the backup path.
- **R4** — `is_git_repo` uses `--show-toplevel` equality (handles linked
  worktrees / `.git` symlinks).

**Accepted + documented (safety-over-fidelity):** D4 (ignored strays), F1 (stray
nested repos) — `clean` deliberately keeps them (no `-x`/`-ff`), so `git status`
can differ; wiping them is worse than the mismatch. Empty untracked dirs aren't
restorable (git limitation). See Non-goals.

**Verified non-bug:** `read-tree --reset -u` force-overwrites untracked
collisions and dir↔file flips (scratchpad proof), so reconstruct needs no
pre-clean.
- **P5.4 polish — SHIPPED.** `restore_backup()` + `fsync git restore` (one-command
  undo of an apply, from the bundle+meta / tar / ignored-copies), and pre-apply
  backup **retention** (`--keep N`, default 10). See Meta-synchronization below
  for the code/config coordination layer.
- **P5.4 — (Roadmap, still out of scope).** Incremental bundles (`<base>..HEAD`,
  needs a basis ref) + a state layer (sqlite sidecar → central Postgres control
  plane per `fsync-control-plane`) enabling **divergence detection** and thus
  optional bidirectional git-sync. This is where ideas (a) Postgres and
  incremental transport return, once v1 proves the transport.

## Meta-synchronization

Rolling out a capability like `kind: git` changes **both code and config on both
boxes** — and if they drift (config reaches a box before the code, or vice
versa) you get breakage. Meta-sync keeps the sync *tooling itself* coherent:

- **Code propagates for free.** fsync is an *editable* install (`~/.local/bin/fsync`
  → the repo `.venv`), and the repo travels via its own file-profile
  (`workspaces/homelab`). Sync the repo → both boxes run the same fsync. (The
  remote-index engine copy is also refreshed each run by `ensure_peer_engine`.)
- **Config propagates via `include:`.** Split the config: the box-local
  `sync-profiles.yaml` keeps `peer:` and `defaults.direction` and does
  `include: [shared-profiles.yaml]`; the **shared** file holds the profile
  definitions and travels as an ordinary synced file. `load_config` merges them,
  box-local overlaying the shared parts — so editing a profile once and syncing
  propagates it, while each box keeps its own peer/direction.
- **A `requires:` compatibility gate.** The config declares the capabilities it
  needs (`requires: [git-sync]`); `load_config` checks them against this build's
  `fsync.FEATURES` and **refuses with a clear message** if the code is too old —
  turning the skew window into a safe, informative failure instead of a crash.
- **`fsync meta` for visibility.** `meta version` (JSON: version + capabilities +
  config summary), `meta check` (validate local config against this build),
  `meta status` (probe the peer's installed fsync over SSH and report version
  drift + whether each box satisfies the other's config `requires`).

`fsync.__version__` is `0.2.0`; `FEATURES = {home-sync, git-sync, meta-sync}`.

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

## Enabling it (deploy playbook — not yet done on the live boxes)

Coordinated via meta-sync so code and config move together safely:

1. **Ship the code first.** With the p5-git-sync branch merged/checked out on both
   boxes (editable install), each box has `git-sync` in `fsync.FEATURES`. Verify:
   `fsync meta check` on each box, and `fsync meta status` to confirm the peer is
   also up to date (it reports version drift + capability gaps).
2. **Put the shared profiles in an included file** so config propagates by
   file-sync. `~/.config/fsync/shared-profiles.yaml` (synced, identical on both):
   ```yaml
   requires: [git-sync]              # compatibility gate
   profiles:
     primary-repos:                  # capture repos to bundles (not file-sync their .git)
       kind: git
       paths: [workspaces/primary]
       exclude: ['.venv', 'venv', 'node_modules']
     git-bundles:                    # carry the bundle dir between boxes
       paths: ['.fsync/git-bundles']
   ```
   Keep `~/.config/fsync/sync-profiles.yaml` box-local:
   `peer:`, `defaults.direction`, and `include: [shared-profiles.yaml]`.
   Add a synced file-profile for the shared config itself (e.g. include
   `.config/fsync/shared-profiles.yaml` in an existing dotfiles/config profile).
3. **Remove `workspaces/primary` from the old file `primary` profile** so the two
   mechanisms don't both touch the repos (this also ends the phantom-deletion
   class permanently).
4. On the source box `fsync sync run --all` (snapshots git repos + carries the
   bundle dir); on the other box `fsync git status` then `fsync git apply` (undo
   with `fsync git restore`).

The `requires: [git-sync]` line means if the shared config reaches a box whose
fsync is older, `load_config` refuses with a clear upgrade message rather than
misbehaving — code and config stay coupled.
