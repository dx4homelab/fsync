# Hub sync (P7) — requirements & design draft (v1, 2026-11-08)

Option C from the 2026-11-08 architecture brainstorm: replace the two-box
peer-to-peer topology with **one hub + N spokes**, where the hub is an active
fsyncd role that owns a canonical tree, per-path state, tombstones and a change
journal. Spokes only ever talk to the hub. Builds on
`docs/home-sync-automation.md` (P4.1 daemon, R1–R10), `docs/git-repo-sync.md`
(R13+), `docs/meta-deploy.md` (P6, R20–R26). Requirement numbers continue at R30.

## Context

Three development workstations now share one `$HOME` carry-set:

| Box | Role today | Mobility |
|---|---|---|
| minis4dx | stationary, always on LAN, fsync source + deploy origin, "producer" (push) | never moves |
| fury4dx | laptop, "consumer" (pull), travels to client site for days | wired/wifi, often away |
| omen4dx | new laptop (2026-11-07), not yet in fsync; Tailscale pending | mobile |

What the current design cannot absorb:

1. **One `peer:` per host.** `hosts.<name>.peer` is singular; the engine, `peer_reachable`,
   `peer_busy`, `ensure_peer_engine`, address probing, SSH trust and the mTLS trust store are
   all written for exactly one counterpart.
2. **One-way is load-bearing.** The churny profiles (`homelab`, `primary`, `repos`) are one-way
   *because* file sync has no tombstones: two-way resurrected deleted files from the stale side
   ([[fsync-stale-mirror-ghosts]], 2026-07-11). One-way only works while exactly one box edits.
   With three dev boxes there is no single producer.
3. **No node knows what another already has.** Pairwise newest-wins between three live trees
   ping-pongs; stale git bundles re-applied over fresh commits (the auto-apply stomp) is the
   same defect: the sender re-sends what the receiver already moved past.
4. **O(N²) plumbing.** SSH keys, pinned host keys, pinned TLS certs, `addresses:` probe lists
   and deploy targets all grow per pair.

A mesh (option A) keeps 1–4. A passive hub tree (option B) fixes 1 and 4 only. This doc is the
active hub (option C), staged so each phase ships on its own.

## Goals

- Any box can be edited; changes reach every other box through the hub.
- Deletes and renames propagate as first-class operations, reversibly (backup, never `rm`).
- A laptop absent for days replays exactly what it missed, in order, once.
- A spoke needs reachability to the hub only (LAN or Tailscale), never to another spoke;
  the hub never initiates connections to spokes.
- One execution code path (CLI, timer, daemon, TUI all run the same `fsync sync run`), as today.

## Non-goals (v1)

- Content-addressed blob store / dedup on the hub (plain tree + state DB is enough).
- Real-time (inotify) sync. Runs stay scheduled/opportunistic.
- Replacing the file engine with Syncthing (option D). Decision deferred to the end of P7.2;
  the hub state model is designed so that swap stays possible.
- Multi-user. One owner, one `developer` account everywhere.

## Requirements

- **R30 — Star topology.** Config declares one `hub` and N `spokes`. A spoke's run syncs
  `local $HOME` ↔ `hub tree` only. The hub box may also be a spoke (minis4dx is both).
- **R31 — Hub owns its tree.** The hub tree (`~/.fsync/hub/<profile>/tree`) is written only by
  hub-mediated runs. It is *not* anyone's live `$HOME`, so every change to it is journaled.
  (Rejected alternative: use minis4dx's live home as the hub — its local edits would bypass the
  journal and the hub could never state what each spoke has.)
- **R32 — Base-aware three-way compare.** Each spoke keeps, per profile, the hub state it last
  converged to (the *base*). A run classifies each path by (base, local, hub): local-only change,
  hub-only change, both changed, local delete, hub delete. Today's prev-index resurrection guard
  (`detect_resurrections`) is the seed of this.
- **R33 — Tombstones.** A delete observed on a spoke becomes a tombstone on the hub (path, hash
  at deletion, origin host, seq). Other spokes apply it by moving the file to their backup dir.
  A spoke that re-creates the path *after* the tombstone's seq wins (new content, new seq).
  Tombstones compact after `retention` (default 90 d).
- **R34 — Journal + cursors.** Every accepted hub change appends `(seq, profile, path, op,
  hash, size, mtime, origin, ts)`. Each spoke stores `last_seq` per profile. "What changed since
  I was last here" is one query, and nothing is ever replayed twice. This retires both
  `.fsync/git-bundles` / `.fsync/svn-snapshots` as *carrier* profiles and the sticky
  auto-apply refusal bookkeeping.
- **R35 — Conflict policy unchanged, placed once.** `conflict: newer` resolves at the hub for
  both-changed paths; the loser goes to a backup dir on the box that is overwritten (R4).
  `conflict: review` holds the path on the hub (visible to all spokes) instead of per box.
- **R36 — Per-profile lease.** The hub grants one spoke at a time a lease per profile (TTL,
  renewed during transfer). Replaces `peer_busy`. A crashed spoke's lease expires; no manual
  unlock.
- **R37 — Bytes over rsync, control over the daemon API.** Index exchange, plan, journal,
  cursors and leases go through fsyncd's existing TLS/mTLS listener (pinned certs, `certs.py`).
  File bytes still move with the proven `rsync --files-from` legs over SSH to the hub box.
  Spokes therefore need SSH + TLS trust to the hub only.
- **R38 — Away-capable.** `hub.addresses` is probed in order (LAN name, then Tailscale name).
  The pinned hub certificate is validated regardless of address. Away runs become normal runs;
  R6's "clean no-op when away" applies only when the hub is unreachable on all addresses.
- **R39 — Hub-hosted git and svn stores.** `kind: git` snapshots push to bare repos on the hub
  (`~/.fsync/hub/git/<rel-path>.git`, refs `refs/fsync/<host>/<branch>` + a meta JSON per
  snapshot); `kind: svn` snapshots land in `~/.fsync/hub/svn/<rel-path>/<host>/<seq>/`. Receivers
  fetch from the hub, which knows ancestry, so a stale snapshot can never roll back a newer tree.
  Client WIP still never touches an external remote (git-repo-sync context); the hub is a
  personal box.
- **R40 — Dependency-free spoke core.** The spoke-side client (index upload, journal pull,
  commit) uses stdlib `urllib` + `ssl` with the pinned cert so it fits the P6 `.pyz`. The hub
  role runs inside the existing fsyncd daemon venv (fastapi already there).
- **R41 — Auditable as today (R7).** Each spoke run writes its report under
  `~/.fsync/runs/<run_id>/`; the hub additionally keeps `~/.fsync/hub/runs/` with per-spoke
  commit summaries, so `fsync hub status` answers "which box is behind on what".
- **R42 — Bootstrap from the current producer.** The hub tree is seeded from minis4dx's current
  `$HOME` (it is the up-to-date side by construction of today's one-way flows). Spokes then
  join with an initial run whose base is empty; first-run resurrection candidates are
  reported, not applied.

## Architecture

```
          ┌───────────── hub box (files4dx) ─────────────┐
          │ fsyncd  (hub role, daemon venv, :7444 mTLS)   │
          │   ├─ ~/.fsync/hub/<profile>/tree   (R31)      │
          │   ├─ ~/.fsync/hub/state.sqlite     (R32-R36)  │
          │   │     paths · tombstones · journal · cursors · leases
          │   ├─ ~/.fsync/hub/git/*.git        (R39)      │
          │   └─ ~/.fsync/hub/svn/…            (R39)      │
          │ sshd  (rsync legs land here)                  │
          └───────────────▲───────────────▲───────────────┘
            TLS ctl +     │               │     TLS ctl +
            rsync/ssh     │               │     rsync/ssh
   ┌──────────────────────┴───┐     ┌─────┴───────────────────┐
   │ spoke fury4dx            │     │ spoke omen4dx            │
   │  fsync (pyz) + fsyncd    │     │  fsync (pyz) + fsyncd    │
   │  ~/.fsync/base/<profile> │     │  ~/.fsync/base/<profile> │
   └──────────────────────────┘     └──────────────────────────┘
   (minis4dx is a third spoke; a hub box that also carries a home would run a local spoke)
```

### Hub state (sqlite via `fsync/db.py`)

```
paths      (profile, path) PK, hash, size, mtime, seq, origin, deleted_at NULL|ts
journal    seq PK autoinc, profile, path, op ∈ {put, del, hold}, hash, size, mtime, origin, ts
cursors    (host, profile) PK, last_seq, last_run_id, last_seen
leases     profile PK, host, run_id, expires
holds      (profile, path) PK, hub_hash, incoming_hash, incoming_origin, since   -- conflict: review
```

### Hub API (fsyncd additions, all behind the existing mTLS dependency)

```
POST /v1/hub/{profile}/lease              acquire/renew  → {lease_id, expires}
DELETE /v1/hub/{profile}/lease/{lease_id}
GET  /v1/hub/{profile}/journal?since=SEQ  → [{seq, path, op, hash, size, mtime, origin}]
GET  /v1/hub/{profile}/state?prefix=      → current paths (bootstrap / repair)
POST /v1/hub/{profile}/plan               body: {base_seq, local_delta[]} → {push[], pull[], del_local[], del_hub[], conflicts[]}
POST /v1/hub/{profile}/commit             body: {lease_id, put[], del[]} → {seq_to, rejected[]}
GET  /v1/hub/status                       per-profile head seq, per-host cursor, leases, holds
GET  /v1/hub/git/{rel}/meta  POST …/snapshot   (R39; bytes via `git push` over ssh)
```

The hub verifies every `commit` by hashing the touched paths in its own tree (size+mtime cache
as elsewhere) before appending to the journal; a mismatch is `rejected[]` and the spoke retries
that path next run.

### Spoke run (one profile)

1. Acquire lease (R36); abort cleanly if held or hub unreachable on all addresses (R38).
2. `local_index` (incremental cache, as today) → local set *L*. Load base *B*
   (`~/.fsync/base/<profile>.json.gz`, written at the end of the last converged run) and the
   hub delta *H* = journal since `last_seq`.
3. Classify each path (R32):

   | B | L | H | action |
   |---|---|---|---|
   | = | changed | — | push |
   | = | — | changed | pull |
   | = | changed | changed | `conflict` policy → push or pull, loser → backup |
   | present | missing | — | hub tombstone (`del`), hub copy → hub backup |
   | present | = | tombstone | local → backup dir, then remove |
   | present | changed | tombstone | local wins if local mtime > tombstone ts, else backup+remove |
   | missing | new | new | same content → no-op; else `conflict` policy |
   | missing | new | — | push (first-run introductions reported, see R42) |

   Renames are rename pairs on both sides (ISSUE-002 fix carried over).
4. Transfer: one `rsync --files-from` push leg and one pull leg (existing `run_rsync_leg`,
   `TOLERATED_RSYNC_EXITS`), hub-side `--backup-dir ~/.fsync/hub/backups/<run_id>/<profile>`,
   local `--backup-dir ~/.fsync/backups/<run_id>/<profile>`.
5. `commit` the pushed puts and dels; receive `seq_to`.
6. Write report, new base = (hub state as of `seq_to` restricted to this profile), cursor =
   `seq_to`. Release lease.

Failure anywhere before 6 leaves base/cursor untouched; the next run re-derives the same plan
minus what already landed (hashes equal → no-op). This is the same idempotence the rsync legs
have today.

### Kinds

| kind | today | with hub |
|---|---|---|
| files | pairwise rsync, additive, prev-index guard | 3-way vs base, tombstones, journal (above) |
| git | snapshot → bundle file → carried by `git-bundles` file profile → receiver auto-apply with sticky refusals | snapshot → `git push` to hub bare repo (`refs/fsync/<host>/*`); receiver fetches from hub; apply guards (clean / same branch / fast-forward-aware / backup) unchanged; stale replay impossible because the hub ref already moved |
| svn | snapshot → files in `.fsync/svn-snapshots` → carried by file profile → `fsync svn apply` | snapshot uploaded to hub store, journaled as `put` of a virtual path; same on-demand/auto apply |
| claude (`~/.claude` durable artifacts) | both, newer, `merge_jsonl` | files kind through hub; `merge_jsonl` runs at the hub on both-changed `history.jsonl` |
| claude-context (handoffs, hooks) | both, newer; "once per box" in the restore hook | files kind through hub; cursor makes "unconsumed per box" derivable later, hook unchanged for now |
| dotfiles / secrets / documents / tools | both, newer | unchanged semantics, now N-way |

### Security / trust

- TLS: reuse `certs.py` exactly. Hub pins every spoke cert in `tls/trust/`; each spoke pins the
  hub cert. No CA. Adding omen4dx = exchange two PEMs + one ssh pubkey, once.
- SSH: spokes hold a key accepted by `developer@hub`; the hub holds none for spokes. Laptops
  therefore expose nothing inbound.
- Tailscale: only the hub needs to be on the tailnet for away sync; spokes reach it by MagicDNS
  name listed in `hub.addresses`.

### Hub placement

**Decision 2026-11-08 (user): the hub is files4dx**, the always-on file server — not minis4dx as
the first draft below assumed. Measured on files4dx the same day: Fedora CoreOS (uCore HCI),
Python 3.14.6, rsync 3.4.1, OpenSSL 3.5, sshd on :22, 16 cores / 31 GB; `$HOME` is on the local
NVMe (`/var`, xfs, 238 GB free), the 65 TB encrypted `tank` is unlocked with its key file in `/etc`
on that same local disk. The hub tree (≈ 41 GB) therefore lives on files4dx's **local NVMe** under
`~/.fsync/hub` — it does not depend on the tank being unlocked, which retires the earlier
"offline pool key would lock the hub" objection; the tank stays available later for hub backups
or the P7.3 git/svn stores if they outgrow the NVMe. files4dx runs the hub role only
(`role: [hub]`): it has no carry-set of its own, so its daemon serves leases/state and `fsync sync
run` there is a gated no-op. minis4dx becomes an ordinary spoke and stays the deploy origin
(`fsync deploy push` targets files4dx like any other host). The same `developer@bluefin` ssh key
is already authorized on files4dx from both minis4dx and fury4dx.

Superseded draft reasoning, kept for the record: minis4dx was proposed because it is always on,
already the deploy origin and today's producer, with the tank NFS as the fallback for the tree.

### Deploy (P6 interplay)

Unchanged in P7.1–7.2: `fsync deploy push` from minis to each spoke (`hosts.<name>.ssh`).
P7.3 optional: hub serves the current pyz + config at `GET /v1/hub/artifact`, spokes run
`fsync self-update` at the start of a scheduled run so a config change reaches laptops the next
time they see the hub, including from the client site.

## Config v2 (`~/.config/fsync/sync-profiles.yaml`)

```yaml
version: 2
requires: [git-sync, svn-sync, hub-sync]

hub:
  host: minis4dx                 # which hosts: entry runs the hub role
  root: ~/.fsync/hub
  addresses: [minis4dx.lan, minis4dx.tail-xxxx.ts.net]   # probed in order by spokes
  ssh: developer@minis4dx.lan    # rsync target; address substituted per probe result
  retention: {tombstones: 90d, backups: 60d, runs: 200}

hosts:
  minis4dx: {ssh: developer@minis4dx.lan, role: [hub, spoke]}
  fury4dx:  {ssh: developer@fury4dx.lan,  role: [spoke]}
  omen4dx:  {ssh: developer@omen4dx.lan,  role: [spoke]}

shared:
  defaults: {conflict: newer, rename_min_size: 64, workers: 8, backup_root: ~/.fsync/backups}
  profiles:
    # all existing profiles keep their paths/excludes verbatim.
    # `direction:` is dropped: every file profile is spoke<->hub two-way once P7.2 lands.
    # P7.1 transitional knob per profile: mode: additive (today's behaviour) | full (tombstones)
    homelab: {paths: [workspaces/homelab], exclude: [...], mode: additive}
    repos:   {kind: git, apply: auto, paths: [workspaces/primary, workspaces/homelab], exclude: [...]}
    # git-bundles / svn-snapshots carrier profiles are removed in P7.3
```

`_select_host_config` keeps selecting by hostname; `Peer` is replaced by `Hub` (addresses,
ssh, root, pinned cert) and `role` decides whether fsyncd starts the hub routes.

## Phases

### P7.0 — Prep (no behaviour change)
- Measure the carry-set: `du` of every profile's paths on minis4dx minus excludes; decide
  local disk vs tank NFS for the hub tree.
- Decide the two policy questions in *Open questions*.
- Add omen4dx to the *current* v1 config as a pull-only consumer so it is not blocked on P7
  (`docs/omen4dx-laptop.md` TODO).

### P7.1 — Star topology, additive (ship first)
- Config v2 loader (v1 still accepted); `Hub` object; `fsync hub init` seeds the tree from the
  local home per profile; `fsync hub status`.
- fsyncd hub routes: lease, status. Spoke run = today's engine with the hub tree as the remote
  endpoint, both legs, `mode: additive` for all profiles (no tombstones yet; prev-index guard
  stays).
- minis4dx runs hub + local spoke; fury4dx and omen4dx join as spokes; Tailscale address in
  `hub.addresses`.
- Acceptance: a file created on any box appears on both others after each has run once; the
  pairwise `peer:` code path is deleted; no spoke holds another spoke's ssh/TLS trust.

### P7.2 — State, tombstones, three-way
- sqlite state, journal, cursors, base files; `plan` and `commit` routes; classification table
  above; `mode: full` default, `additive` kept as an escape hatch.
- `conflict: review` holds move to the hub (`GET /v1/conflicts` lists hub holds).
- Acceptance (three homes on one box, hub on localhost — extend `vmclone.py` / `tests/`):
  delete on A → gone on B and C, present in their backup dirs; re-create on B after the delete
  → present everywhere; both-changed → newer wins, loser in backup on exactly one box; laptop
  "away" for 50 journal entries replays once, cursor advances once.

### P7.3 — Kinds move to hub stores, self-update
- git: bare repos on hub, `refs/fsync/<host>/*`, fetch-based apply; delete `git-bundles`
  carrier, bundle dir, sticky refusal file. svn: hub snapshot store; delete `svn-snapshots`
  carrier.
- `GET /v1/hub/artifact` + `fsync self-update`.
- Acceptance: the auto-apply stomp scenario (stale snapshot vs fresh commits) is a no-op;
  `~/.fsync/git-bundles` and `~/.fsync/svn-snapshots` are gone on every box.

### Code map

| Module | Change |
|---|---|
| `homesync.py` | `Peer` → `Hub`; `run_profile` gets the 3-way classifier; base read/write; `peer_busy` → lease client; `detect_resurrections` folds into the classifier |
| `daemon.py` | hub routes (R36, journal, plan, commit, status); `role` gate at startup |
| `db.py` | hub schema + migrations |
| `client.py` | spoke-side hub client, stdlib only (R40) |
| `certs.py` | unchanged; `fsync certs trust` gains `--hub` |
| `git_repo_sync.py`, `svnsync.py` | P7.3: hub store transport instead of file carriers |
| `meta_deploy.py` | config v2 awareness; P7.3 artifact route |
| `cli.py` | `fsync hub init|status|gc`, `fsync self-update` |
| `tests/` | three-home fixture with hub on 127.0.0.1 |

## Open questions (answer in P7.0)

1. **Same project on two boxes within one interval?** If never, `newer` at the hub is enough for
   `homelab`/`primary`. If sometimes, those two profiles get `conflict: review` with hub holds.
2. **Hub tree location:** minis4dx disk vs `files4dx-tank` NFS (depends on the P7.0 `du`).
3. **Syncthing for the files kind?** Revisit after P7.2 acceptance: if the classifier + tombstone
   work proves heavier than expected, the hub state model maps onto a Syncthing folder with
   fsync keeping git/svn/claude/deploy.
4. **Run cadence per spoke:** hourly timer as today, or hub-side "pending for you" poll from the
   spoke daemon every few minutes (cheap once journal exists).

## Implementation status

### P7.0 — answers (2026-11-08)

- **Carry-set size (minis4dx, `du` minus the profile excludes):** homelab 17 G, primary 22 G,
  Documents 1.2 G, .claude 347 M, tools 330 M, Pictures 310 M, git-bundles 129 M, secrets 93 M;
  ≈ **41 GB** against 278 GB free on `/var/home`. → **Hub tree on minis4dx's local disk**
  (`~/.fsync/hub`); the tank NFS option is not needed. (Open question 2 — closed.)
- Open question 1 (same project on two boxes within one interval) and 4 (cadence) stay with the
  user; P7.1 keeps `conflict: newer` and the hourly daemon scheduler.
- omen4dx is still not in the config (no ssh/TLS trust yet; Tailscale pending) — add it as a
  spoke when it is reachable; nothing in P7.1 blocks on it.

### P7.1 — shipped in fsync 0.8.0 (2026-11-08), feature flag `hub-sync`

Code (all stdlib on the spoke side, R40):

| Module | What landed |
|---|---|
| `homesync.py` | `Hub(Peer)` (root, roles, retention, `local`), config v2 loader (`version: 2` / `hub:` + per-host `role:`; v1 untouched), `remote_root_for` / `remote_backup_for` / `hub_root` / `hub_tree` as the one place a counterpart path is decided, **local mode** (`Peer.local`: `_ssh` → bash, `remote_index` → `local_index`, rsync without `host:`, engine push skipped) so the hub box runs a plain spoke run against its own tree, hub-tree `mkdir -p` before the legs, `_run_file_profile` = `run_profile` under a per-profile lease + `record_run`, `peer_busy` → False for hubs |
| `hubstate.py` | sqlite `<root>/state.sqlite`: `leases` (TTL, renew, steal-on-expiry — R36), `spoke_runs` (last run per host×profile — R41 seed of the P7.2 cursors), `Lease` context manager with a TTL/3 renew thread, `LocalHubControl` |
| `hubclient.py` | urllib+ssl client pinned to the hub cert (`check_hostname` off → same pin on LAN and tailnet, R38), ordered-address failover, `RemoteHubControl` |
| `hub.py` | `hub_control()` picks local sqlite vs pinned-TLS API (mTLS port from the shared `daemon:` block), `fsync hub init [--seed]`, `fsync hub status [--json]` |
| `daemon.py` | `register_hub_routes` mounted only when this box's config says `role: [hub, …]`: `POST /v1/hub/{profile}/lease` (acquire / renew → 409 held / 410 lost), `DELETE …/lease/{id}`, `POST …/runs`, `GET /v1/hub/status`; `/v1/peer` skips the self-relay on the hub box |
| `cli.py`, `meta_deploy.py`, `foldersync.py` | `fsync hub` subcommand; `hub.py`/`hubstate.py`/`hubclient.py` in `CORE_MODULES` (pyz); `fsync sync folder` refuses a hub config until P7.2 |
| `tests/test_hub_sync.py` | 21 tests incl. the acceptance scenario on one box: two homes + local hub → a file created on either appears on the other after each ran once; a profile leased elsewhere is *deferred*, not failed; overwrite through the hub backs the loser up under `<root>/backups` |

Deviations from the draft above, on purpose:

- **`direction:` is still honoured in v2** (default `both`). The draft drops it only "once P7.2
  lands": without tombstones, additive two-way through the hub resurrects deletes exactly like the
  2026-07-11 incident, so the churny profiles (`homelab`, `primary`) can stay one-way per host
  (`defaults: {direction: push}` on minis, `pull` on the laptops) until `mode: full` exists.
  `kind: git` / `kind: svn` keep their producer/receiver gates for the same reason (P7.3 moves them).
- **Leases are per profile, taken inside the run loop.** One profile in flight on another spoke
  defers only that profile (`status: deferred`, exit 0); the hub control plane being down is a
  profile error, never a silent pairwise fallback.
- **The hub box's own spoke run talks to sqlite directly** (`LocalHubControl`), the daemon and the
  CLI share the file; spokes go through the daemon's mTLS listener. No second lease mechanism.
- **R42 seeding = an ordinary run**: `fsync hub init --seed` is `hub init` + `sync run --all` on the
  hub box (everything local flows into the empty tree; nothing comes back).
- Known limitation until P7.2/P7.3: the ISSUE-010 svn stale-sender guard compares a sender
  checkout with the *receiver's* checkout; through the hub there is no receiver checkout (the hub
  tree has `.svn` excluded), so the guard reports only. The durable answer stays `svn` profile
  `apply: auto` (file profiles skip the checkouts).

### P7.2 — shipped in fsync 0.9.0 (2026-11-08), feature flag `hub-full`

| Piece | What landed |
|---|---|
| `hubstate.py` (schema 2) | `paths` (live rows + tombstones), `journal` (put/del/hold, global seq), `cursors`, `holds`; `commit()` verifies every put against the tree (size + mtime) before journaling, moves deleted tree files to `<root>/backups/<run>/<profile>` (never rm), refuses a stale lease; `index_tree()` bootstraps/repairs the table from a tree index and marks the profile *indexed*; `gc()` compacts tombstones + journal past retention and records a per-profile journal floor; `state()` / `journal_since()` (prefix-scoped, compact lists) |
| `threeway.py` | pure classifier for the R32 table (first run, both-changed, delete-vs-edit, tombstone-vs-recreate by mtime, `merge_jsonl`, `review`), direction as a routing filter, **mass-delete guard**, the base file (`~/.fsync/base/<profile>/<key>.json.gz` = hub view at seq + tombstones + *converged overrides*) |
| `homesync.py` | `Profile.mode` (`full` default under a hub, `additive` escape hatch); `_run_path_full`: journal-since-base (or full state) → classify → push/pull legs → local removals to the backup dir → hub commit → pull verification → new base; preview uses the same planner; `--allow-mass-delete` |
| `daemon.py` / `hubclient.py` | `GET …/state`, `GET …/journal?since=`, `POST …/commit`, `GET …/holds`, `GET /v1/hub/holds`; status carries head seq, live/tombstone counts, holds and per-spoke cursors |
| `hub.py` | `fsync hub index [--profile]`, `fsync hub gc [--dry-run]`, `fsync hub holds [--release P PATH]`; status shows `seq · live · tomb · HELD` and each spoke's cursor gap |
| `tests/test_hub_p72.py` | 13 tests: the classifier table row by row, commit verification/rejections/tombstones/holds, index + gc + journal floor, and the acceptance scenario on one box — delete on A → gone on B and C with backups; re-create on B → everywhere (revived); both-changed → newer wins, loser backed up on exactly one box; laptop away for 50 changes replays once and the cursor advances once; pull-only never pushes or tombstones; `review` holds on the hub; the mass-delete guard; refusal against an unindexed hub; dry-run and preview |

Design notes beyond the draft:

- **The base is two things.** The hub's exact view at `seq` (so the journal can be replayed on top)
  plus *overrides* for paths the run did not converge (held, stranded, rejected, failed pull):
  the classifier's B uses the override, the replay uses the view. Without the split a held
  conflict looked like "local changed only" one run later and would have pushed over the hub.
- **Tombstones ride in the base** so a re-creation is recognised as a revival and judged against
  the delete's timestamp even after the journal compacted.
- **Mass-delete guard.** ≥ 500 local deletions that are also ≥ 20 % of the base are held
  (reported, `status: attention`); `--allow-mass-delete` applies them. An absent local root never
  derives deletions at all. This is the `rsync --max-delete` of the hub world.
- **Direction stays a routing filter** (push-only never pulls or removes locally, pull-only never
  pushes or tombstones; withheld actions are `stranded`). Flip a profile to `both` when its boxes
  should all edit — the resurrection reason for one-way is gone in `mode: full`.
- **A hub profile must be indexed** before spokes may run `mode: full` against it (`hub init` marks
  trees it created empty; a tree that predates P7.2 needs `fsync hub index`). Otherwise every
  spoke would re-push the world as "introductions". The spoke refuses with a clear error.
- `kind: git` / `kind: svn` carriers are untouched (P7.3). `fsync sync folder` still refuses hubs.

### P7.3 — shipped in fsync 0.10.0 (2026-11-08), feature flag `hub-stores`

| Piece | What landed |
|---|---|
| `git_repo_sync.py` | `snapshot_repo_to_hub`: capture as before, then `git push --force` branches, tags and the WIP ref into the hub's bare repo under `refs/fsync/<host>/…` (the producer's own namespace, pruned of branches it dropped); `_fetch_incoming` / `apply_repo` take a hub URL + namespace instead of a bundle file; all guards (busy, dirty, branch divergence, receiver-ahead pre- and post-fetch, R15 backup) unchanged; `GIT_SSH_COMMAND` carries the fsync ssh policy |
| `hubstate.py` / routes | `kind_snapshots (kind, profile, rel, host) → meta, ts` registry; `POST /v1/hub/kind/{git\|svn}/snapshot`, `GET …/snapshots`; status lists per-store hosts and ages; `GET /v1/hub/artifact[/pyz\|/config]` |
| `homesync.py` | snapshot/auto-apply/CLI for both kinds go through the hub when the config has one: a receiver applies the **newest snapshot another box registered** (never its own); svn snapshot files rsync to `<hub>/svn/<rel>/<host>/` and are mirrored into a local staging dir for the unchanged `svnsync.apply_wc`; `fsync deploy push` publishes the pyz + config + manifest to `<hub>/artifact/` |
| `hub.py` | `hub_git_url`, `ensure_hub_bare`, `hub_svn_path`, `publish_artifact`, `install_artifact` (backup-first, sha256-verified, `meta version` check, rollback), `fsync self-update [--check\|--force]` |
| `tests/test_hub_p73.py` | 5 tests incl. the acceptance scenario: producer → hub → receiver (HEAD, branch, dirty file, untracked file intact); receiver commits ahead, producer re-snapshots its old state → receiver **skips as ahead, untouched** (the stomp is a no-op); producer catches up → "up to date"; new producer commit flows; `fsync git status/apply` through the hub; registry newest-wins per repo; artifact publish/route/install |

Notes: the `git-bundles` and `svn-snapshots` **carrier profiles are retired** (removed from the live
config 2026-11-08; the old dirs were moved aside as `*.retired-20261108` on every box and under the
hub root — delete them when satisfied; `fsync hub gc --drop-profile NAME` forgets their hub state);
nothing is written to `~/.fsync/git-bundles` any more; `~/.fsync/svn-snapshots` stays as
the producer's local staging dir and the receiver's `hub/<host>/<name>/` mirror. The sticky-streak
escalation for repeated skips is kept (it is the receiver-ahead signal a human must act on).
`fsync self-update` updates the pyz + config of a spoke from the hub; the fsyncd venv still comes
from `fsync deploy daemon` (wheelhouse), so a code change still needs that step. files4dx has git
2.55 and no svn (the svn store is plain files; `svn` runs only on the spokes).

### Direction flip — 2026-11-08 evening

`homelab` and `primary` are `direction: both` on every box (tombstones make it safe); `svn` is
`apply: auto`, so file profiles skip the svn working copies on each box (25 on minis, 27 on fury) and
checkouts travel only through the guarded `kind: svn` path. `repos`/`svn` keep the host default
direction (minis producer → laptops): a checkout or repo that exists **only on a laptop** (e.g.
fury's `afsas/afsas-tomcat-11-migration`) therefore does not reach the other boxes yet — flipping
the kinds to `both` needs the auto-apply gate to accept "snapshot and apply others' newest" on one
box, which is the next step if wanted. Open question 1 is thereby answered "newest wins at the hub".

### Cutover — EXECUTED 2026-11-08 (hub = files4dx, three spokes live)

Operator steps (refused to the automation, done by hand): firewalld `7446/tcp` on files4dx,
`loginctl enable-linger developer` on files4dx and omen4dx, omen4dx's ssh key authorized on
files4dx. Everything else ran from this session: pyz + daemon venv on files4dx and omen4dx,
`fsync hub init` (ten trees), cert pins (hub ↔ each spoke), v2 config live on all four boxes
(v1 kept as `sync-profiles.yaml.bak-v1-*`).

| Run | Box | Duration | Result |
|---|---|---|---|
| seed `20261108-111000-0b8b` | minis4dx | 6 m 46 s | 0 errors; 33 GB pushed (homelab 18.8 G, primary 11.4 G, documents 1.5 G, dotfiles 0.6 G, tools 0.3 G …); hub tree 31 G on files4dx NVMe |
| join `20261108-111717-308c` | omen4dx | 8 m 06 s | 0 errors; 32 GB pulled over WiFi; 32 git repos auto-applied; 11 pre-existing local files withheld (pull-only) |
| join `20261108-112008-1c60` | fury4dx | 34 s | 0 errors; homelab/primary **deferred** (leases held by omen4dx and minis4dx's hourly run at that moment) |
| rerun `20261108-112746-a416` | fury4dx | 1 m 13 s | homelab 12 pulled; primary identical 121,740; stranded = fury's own pull-only work (known backlog) |

Observed live: three spokes converging concurrently, each holding a different profile lease;
a file omen4dx pushed into the hub (`dotfiles`, `claude`) reached fury4dx on its next run with no
spoke-to-spoke contact (P7.1 acceptance). `fsync hub status` on any box lists all three spokes.

Two findings fixed the same day: the deployed pyz lacked `certs.py` (`fsync hub` could not import
on a spoke — the import check had run from the repo dir, which shadows the pyz); and the seed
reported ~160 k "resurrected" files because the peer-side prev-index baseline still described the
v1 peer's tree — the hub baseline is now scoped `<key>.peer@hub-<host>` (one-time, no data effect).
Expected carry-overs: fury's `attention` on `claude` (24 `.claude/state/*` ephemera re-created from
the hub — additive until P7.2) and its stranded pull-only work (user reconciliation, as before).

### Cutover checklist (reference; hub = files4dx)

1. **Install fsync on files4dx.** Add `files4dx: {ssh: developer@files4dx.lan, role: [hub]}` to
   `hosts:` and switch the shared config to v2 in one edit on minis: top-level `version: 2`,
   `requires: [git-sync, svn-sync, hub-sync]`, `hub: {host: files4dx, ssh: developer@files4dx.lan,
   addresses: [files4dx.lan], root: ~/.fsync/hub}`, `role: [spoke]` under minis4dx/fury4dx, drop
   every `peer:` block, keep the per-host `defaults: {direction: …}` overlays and all profiles
   verbatim. Then `fsync deploy push` (pyz + config to fury4dx and files4dx — minis accepts
   files4dx's host key on first contact) and `fsync deploy daemon` (daemon venv on all three;
   files4dx has `python3 -m venv` + ensurepip). `fsync hub init` on files4dx lays down the trees.
2. **Trust.** On each spoke: `fsync daemon trust developer@files4dx.lan --name files4dx`; on
   files4dx: `fsync daemon trust developer@minis4dx.lan --name minis4dx` and the same for fury4dx
   (its mTLS listener starts once it pins at least one spoke). `fsync hub status` from a spoke then
   answers over TLS.
3. **Seed (R42).** `fsync sync run --all` on minis4dx: the first run pushes the whole carry-set
   (≈ 41 GB over the wired LAN) into the empty hub tree; nothing comes back. Then fury4dx runs.
4. **Acceptance (P7.1).** A file created on any box is present on the others after each ran once;
   `fsync hub status` on files4dx lists both spokes; no spoke holds another spoke's ssh/TLS trust
   (the pairwise `minis4dx.pem`/`fury4dx.pem` pins can be removed from the spokes).
5. **Scale.** Add omen4dx as `role: [spoke]` with the same two trust commands once it is on the LAN
   or files4dx is on the tailnet (`hub.addresses` then gains the MagicDNS name).
