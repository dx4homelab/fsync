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

## ISSUE-002 — Detected "renames" are dropped from profile-driven syncs entirely: never copied, never renamed, still `status: ok`

- **Status:** **FIXED in 0.6.0** (2026-07-28) — fixes **A + C** below. Was: product bug, verified
  in code; incident resolved by hand the same day.
- **Where seen:** `workspaces/primary/asap/scratchpad/collision-check/`, `primary` profile.
  `renames_pending: 5` in **every** run from 2026-07-13 to 2026-07-28.

### Symptom
Five files existed only on fury (dated 07-12) and five only on minis (dated 07-13). Neither set
ever crossed, for fifteen days, while the profile reported `conflicts: 0`, `identical: 170551`,
`status: ok`. The only trace was `renames_pending: 5`, which never changed and never escalated.

The files are the S3 **collision-check fixtures** — each body reads
`{"submissionType":"hazard","bogus_field_2":"payload number 2 — must not be overwritten"}`.
fsync declined to copy them at all.

### Root cause (verified against code, 2026-07-28)
`build_sync_plan` (`fsync/fileindex.py:610-739`) classifies a pair as a rename when the same
content hash appears under two different paths and that hash occurs **exactly twice** overall
(`:678-685`). Our five pairs satisfy this precisely: `sha256sum` over the ten files yields **five
distinct hashes, each appearing exactly once per box**.

Pairs that fail the uniqueness or `rename_min_size` guard land in `suppressed` and are correctly
**demoted to plain copies** — `a_to_b.extend(a for a, _ in suppressed)` (`:712-713`). Pairs that
*pass* land in `renames` (`:685`) and are returned at `:736`.

**Nothing in the profile-driven path ever consumes `plan["renames"]`.** It is not added to
`a_to_b`, `b_to_a`, `a_delete` or `b_delete` in any branch — mirror or union. Its only reader in
`fsync/homesync.py` is line **1001**, `"renames_pending": len(plan["renames"])`, which merely
counts it. There is no `os.rename`, `shutil.move` or `mv` anywhere in `fsync/`. The sole real
consumer is `fsync/cli.py:533-551`, which writes a `plan.renames.sh` for the **manual**
`sync-plan` workflow — a path the scheduler never takes.

So a pair classified as a rename is silently **excluded from the plan**. "Pending" is not a queue;
nothing will ever drain it.

### The classification is also wrong here
A rename is a *within-box, across-time* event. What fsync sees is a *cross-box, same-instant*
comparison, where "same content under a different name" is equally well explained by two
independent runs of a tool that emits timestamped filenames — which is exactly what happened. With
no history, fsync cannot tell the two apart, and it currently resolves the ambiguity by doing
nothing, which is the one option that can lose data.

### Resolution (the incident)
Copied both sets explicitly in both directions; each box now holds all ten files.

### Fix
- **A. Demote rename pairs for callers that cannot act on them — DONE (0.6.0).**
  `build_sync_plan` gains `allow_renames: bool = True`; with `False` every pair goes to
  `renames_suppressed`, so the content travels as ordinary copies. `_plan_path`
  (`homesync.py`) passes `allow_renames=False`, because a scheduled run has nobody to act on a
  proposal. The default is unchanged, so `sync-plan` still renders `plan.renames.sh` for a human —
  the interactive case where a rename *is* actionable. Chosen over demoting unconditionally in the
  union branch so the CLI's reviewed-rename feature survives.
- **B. Only honour renames where a rename is *provable* — PARTIAL.** Renames now reach only two
  callers: `mirror` mode (one side authoritative) and interactive `sync-plan`. Not enforced
  structurally — a future unattended caller could still omit `allow_renames=False`, which is what
  **C** guards.
- **C. Never report a leak as green — DONE (0.6.0).** `renames_pending > 0` now forces
  `status: attention` and logs `! … rename pair(s) UNACTIONED … this caller should pass
  allow_renames=False`. In practice profile runs can no longer produce a pending rename; this is the
  backstop for the case that let the bug live fifteen days.
- **D. Age out pending renames — NOT DONE, and no longer needed** for profile runs (they cannot
  accumulate pending renames now). Still worth having if another unattended caller appears.

### Verification (2026-07-28)
Unit: `test_rename_proposal_reaches_neither_side` pins the defect shape (a surviving pair is in
`a_to_b`, `b_to_a`, `a_delete` and `b_delete` — *none* of them);
`test_allow_renames_false_demotes_every_pair_to_copies` and
`test_allow_renames_false_still_converges_under_mirror` pin the fix;
`test_scheduled_plan_demands_rename_demotion` pins the caller. Full suite: 204 passed, 1 skipped.

End-to-end, real two-box dry run over a fixture reproducing the collision-check pathology (5 files
per box, each with a byte-identical twin under a different name, 91 bytes so the
`rename_min_size: 64` floor does not pre-empt the rename classification):

| | A→B planned | B→A planned | renames_pending | status |
|---|---|---|---|---|
| HEAD (pre-fix) | **0** | **0** | **5** | `ok` |
| 0.6.0 | 5 | 5 | 0 | `ok` |

*Method note:* on minis `~/.local/bin/fsync` is a symlink into the checkout's `.venv`, so it always
runs the working tree — a "before" run must use a pristine `git archive HEAD` export **executed from
that export's own directory** (`python -m` puts CWD at `sys.path[0]`, which beats `PYTHONPATH`).
A first attempt missed both of these and produced a false "no bug" reading.

### Prevention
`renames_demoted` was 0 and `renames_pending` was 5 in every single report — a stuck non-zero
counter next to a green status. Any counter that means "work not done" should be wired to status.

---

## ISSUE-003 — One-way profiles report `status: ok` while silently stranding content-differing files

- **Status:** **FIXED in 0.6.0** (2026-07-28) — fixes **B + C** below; **A withdrawn** (wrong
  premise, see below), **D not done**. Incident resolved by hand the same day.
- **Where seen:** `primary` profile (`direction: pull` on fury4dx), every run for ~2 weeks.

### Symptom
Every scheduled run reported the `primary` profile as healthy:

```
primary: status=ok  identical=170551  conflicts=0  files_transferred=0
         skipped_by_direction=1052    renames_pending=5
```

Behind that green line, **45 content-differing files of genuine fury-authored work** had never
reached minis — among them the 2026-07-27 IL2 validation report, `validate-traceid-sim.sh`, six
`*.valid-may.json` e2e fixtures, a new JUnit test, nine OPS incident documents, and the newest
`DASHBOARD-CONTRACT.md`. Some had been stranded since 2026-07-12.

### Root cause
1. `fsync/homesync.py:876` initialises `result = {..., "status": "ok"}` and only downgrades it on
   an error. Stranding is not an error, so the status never moves.
2. `fsync/homesync.py:940-945` computes `skipped_by_direction` as a bare `len(...)` of the paths
   dropped by the direction policy. It is reported (`:999`) and appended to the log line (`:1025`)
   but never influences status, and the paths themselves are never listed.
3. The headline number the UI shows is `identical` (`fsync/views.py:97,110,127,155`). At 170,551
   it dwarfs 1,052 skipped and 5 pending by two orders of magnitude, so the row reads as healthy
   at a glance.

The one-way policy itself is correct and was adopted deliberately (ISSUE-001, and the
2026-07-11 stale-mirror-ghosts incident). The defect is that it is **silent**: "I chose not to
copy this" and "there is nothing to copy" are rendered identically.

### Resolution (the incident)
Compared both boxes with `rsync -ain -u` per direction, then re-checked every candidate with
`rsync -c` to separate mtime drift from real content drift (of 514 "real source" pull candidates,
**509 were mtime-only**). The 45 genuinely-stranded files were pushed explicitly. The profile's
direction was **not** flipped — that would reintroduce ISSUE-001.

### Fix
- **A. Split `skipped_by_direction` into identical vs diverging — WITHDRAWN, the premise was
  wrong.** The plan never contains byte-identical files: `compare_file_lists` routes those to
  `exact_matches`, which becomes `plan["noop"]`. Everything in `a_to_b`/`b_to_a` is there *because*
  it diverges, so `skipped_by_direction` was **already** exactly the stranded count — no split, and
  no extra hashing, was needed. The real gap was only that the **paths were discarded** (`a_paths =
  []`) and the count never reached `status`.
- **B. Keep the paths and escalate — DONE (0.6.0).** The withheld paths are captured before the
  list is cleared and reported as `stranded` (sampled to `STRANDED_SAMPLE = 20`, with
  `stranded_truncated`). `stranded` or `renames_pending` sets `status: attention`, a third state
  beside `ok`/`error`.
- **C. Name the remedy — DONE (0.6.0).** The run now logs
  `! <profile>/<path>: N file(s) STRANDED by direction:<dir> — they differ from the peer and were
  withheld, not synced: … | reconcile with: fsync sync folder <path>`, and the summary line says
  `<dir>-only (N STRANDED)` rather than the neutral `(N skipped)`.
- **D. Rank the report by actionability — NOT DONE.** Also partly misdiagnosed: `views.py` never
  rendered `skipped_by_direction`, `renames_pending` or `untracked_introductions` at all — those
  screens are the *plan preview*, not the run report, and the run report is JSON plus the log
  lines fixed in **C**. Surfacing `stranded` in the TUI remains open.

### Verification (2026-07-28)
Unit: `test_one_way_profile_names_stranded_paths_and_escalates` (25 withheld paths → sampled to 20,
`stranded_truncated`, `status == "attention"`, log names the remedy) and
`test_two_way_profile_with_nothing_withheld_stays_ok` as the negative control.

End-to-end, real two-box dry run, `direction: push` with 5 divergent files on the receiving side:

| | skipped_by_direction | stranded | status |
|---|---|---|---|
| HEAD (pre-fix) | **0** | field absent | `ok` |
| 0.6.0 | 5 | 5 paths named | **`attention`** |

The pre-fix `0` is not a typo, and it shows how the two defects compounded: ISSUE-002 had already
removed those files from the transfer lists, so the direction filter had nothing left to count.
Neither mechanism saw them — a silent double miss.

### Prevention
A one-way policy is a decision to *withhold* data. Withholding must be visible, or it becomes
indistinguishable from having nothing to send.

---

## ISSUE-004 — SVN working copies are file-synced although `.git` is excluded; fsync has no SVN awareness

- **Status:** OPEN — worked around by hand 2026-07-28; product change proposed.
- **Where seen:** `workspaces/primary/asap` (`trunk`, `branches/*`, `asap_e2e`,
  `modsec-container-trunk`, `owasp-coraza-proxy` — 5 SVN working copies, one of them dual-tracked
  with git).

### Symptom
- `.svn` internals were **699 of 959** push-side candidates and **551 of 5,263** pull-side.
- fury carried a `branches/.svn` container checkout (r2547) that minis did not have at all,
  manufacturing permanent phantom divergence under `branches/`.
- `owasp-coraza-proxy` sat at **r2486 on fury and r2726 on minis** for days. fury's copy showed
  **16 modified files**, of which **15 were byte-identical to minis' clean r2726 checkout** — the
  work was already committed upstream; the "modifications" were an artifact of a stale WC base.
  No amount of file-syncing could fix that, and file-syncing `.svn` across boxes risks pasting one
  box's WC metadata (absolute paths, pristine bases, `wc.db`) onto another's tree.

### Root cause
`primary`'s exclude list carries `.git` with the comment *"repos' git state travels via the
kind:git 'repos' profile; file-syncing .git caused phantom deletions"* — the exact hazard applies
to `.svn`, but `.svn` was never added, and there is no `kind: svn` counterpart to `kind: git`
(`fsync/homesync.py` handles `kind == "git"` only). So SVN trees fall through to the plain file
engine, which is both unsafe for `.svn` and useless for the thing that actually matters — the
working copy's revision.

### Resolution (the incident)
Resolved through SVN, not fsync: `svn cleanup && svn update` on fury took the coraza WC r2486 →
r2731; 15 of the 16 "modifications" merged away as `G` (already upstream), leaving exactly the one
genuinely-uncommitted file. minis was updated to r2731 the same way. Both boxes now report
identical revisions across all five checkouts.

### Fix (proposed, 0.6.0)
- **A. Exclude `.svn` from file profiles**, for the same reason `.git` is excluded. Do this first;
  it is one line and removes the metadata-pasting hazard.
- **B. Add `kind: svn`**, mirroring `kind: git`: snapshot = `svn info` (URL + revision) plus
  `svn status`/`svn diff` for uncommitted work; apply = `svn update`, then re-apply the diff.
  Uncommitted changes travel as a patch; committed history travels via the server, which is where
  it already is.
- **C. Minimum viable version — a revision-parity advisory.** Even without `kind: svn`, comparing
  `svn info --show-item revision` per checkout across boxes and reporting
  *"asap/owasp-coraza-proxy: fury r2486, minis r2726 — run `svn update`, do not file-sync"*
  would have surfaced this on day one. It is a few lines and needs no transport.

### Prevention
Both boxes now reach the SVN server over the Staging VPN, so the server is the correct transport
for versioned content. fsync should carry only what SCM does not: untracked and uncommitted files.

---

## ISSUE-005 — Generated build artifacts are ~99% of the diff, hiding the 1% that matters

- **Status:** OPEN — config change proposed, not yet applied.
- **Where seen:** `workspaces/primary/asap` (Flutter + Gradle/Eclipse Java), 2026-07-28.

### Symptom
A full content comparison of one project folder produced 5,263 differing files one way and 959
the other. After filtering generated output, the real divergence was **5 files** and **45 files**
respectively — a signal-to-noise ratio of about **1%**. The `primary` profile leg takes ~39s
per run, most of it hashing artifacts that should never have been candidates.

### Root cause
`primary` / `homelab` exclude `.git`, `.venv*`, `node_modules`, `__pycache__`, `*.pyc`,
`.pytest_cache`, `backups`, `mtab`, `.metadata/*` — a list grown incident-by-incident. It has no
entry for the toolchains actually in these trees:

| kind | paths seen | count |
|---|---|---|
| Flutter/Dart | `.dart_tool/`, `.flutter-sdk/bin/cache/`, `build/` | ~4,700 |
| Gradle | `.gradle/`, `**/build/`, `*.lock`, `gc.properties`, `last-build.bin` | ~700 |
| Eclipse/JDT | `.settings/`, `.classpath`, `.factorypath`, `.project`, `bin/main/**/*.class` | ~120 |

These are machine-local, regenerable, and churn on every build, so they diverge permanently and
by design. `.flutter-sdk/bin/cache/` is a vendored SDK — a *downloaded toolchain*, not project content.

### Fix (proposed, 0.6.0)
- **A. Extend the excludes now** (config-only, no code): add `.dart_tool`, `.flutter-sdk`,
  `.gradle`, `.settings`, `.classpath`, `.factorypath`, `.project`, `build`, `bin`,
  `.pub-cache`, `.cxx`, `.idea`, `*.class`, `*.stamp`, `*.dill` to `primary` and `homelab`.
- **B. Ship `preset:` exclude bundles** so this list stops being hand-maintained:
  `preset: [flutter, gradle, eclipse, node, python]` expanding to curated sets fsync owns and
  updates. Profiles then declare intent, not trivia.
- **C. Report artifact share.** If >50% of a leg's candidates match a known-artifact preset that
  the profile has *not* enabled, log a one-line hint naming the preset. The waste is invisible today.

### Prevention
An exclude list that only grows by incident will always lag the toolchain. Presets make the common
case correct by default.

---

## ISSUE-006 — `kind:git` is producer→receiver only, so receiver-authored commits have no path home; persistent skips never escalate

- **Status:** OPEN — worked around by hand 2026-07-28.
- **Where seen:** `repos` profile, `asap__owasp-coraza-proxy`.

### Symptom
Every run skipped the repo with `reason: "receiver has local changes"`. In fact fury (the
*receiver*) held a legitimate commit that minis did not — `458660f`, a documentation correction on
top of minis' `aef11ce`, on both `main` and `waf-validation-930120-fix`. The guard was right to
refuse, but the commit had no route to minis regardless: `repos` inherits the host default
`direction: pull`, so fury is receive-only and never snapshots.

Nine repos were skipped in the same run (`receiver has local changes` ×6, `branch divergence` ×1).
None of it escalates: the `repos` profile carries no `status` at all in the report.

### Root cause
`_auto_apply_decision` (`fsync/homesync.py`, returning e.g. `(False, "receiver has local changes")`
at `:1118` and `(False, "up to date")` at `:1129`) correctly refuses unsafe applies, but a refusal
is terminal — there is no retry, no escalation, and no notion of the receiver having something to
send. The one-way rule was adopted to stop file-level deletes and renames resurrecting from a
stale peer (ISSUE-001), but **git does not have that hazard**: a fast-forward is provably
non-destructive and trivially checkable with `git merge-base --is-ancestor`.

### Resolution (the incident)
By hand: `git bundle create --all` on fury, `scp` to minis, `git fetch <bundle>
'refs/heads/*:refs/remotes/fury/*'`, verified both branches fast-forwardable, then
`git merge --ff-only` and `git branch -f main`. Both boxes now at `458660f`.

### Fix (proposed, 0.6.0)
- **A. Allow `direction: both` for `kind: git`,** applying **fast-forward-only** in the
  receiver→producer direction. Refuse anything non-ff and report it. This would have carried
  `458660f` automatically, with no possibility of clobbering.
- **B. Escalate sticky skips.** If the same repo is skipped for the same reason for N consecutive
  runs, raise it to `status: attention` and say which side is ahead — "receiver has local changes"
  read identically on day 1 and day 15.
- **C. Give the `repos` profile a `status` field.** It currently reports `applied`/`skipped` lists
  and no status, so it cannot surface in a summary view at all.

### Prevention
One-way is the right default for *files*, where deletes and renames are ambiguous. For git it is
too strict: ff-only exchange is safe in both directions and is what the two boxes actually need.


### Sub-finding — auto-apply can *regress* a receiver that is ahead (no ancestry check)

> **FIXED 2026-08-27** — after the predicted regression happened for real: fury's auto-apply
> rolled `refactor4homelab` back to `0588039` repeatedly, discarding four receiver-authored
> commits (recovered from reflog + pre-apply backups). Implemented as a two-layer guard:
> `check_receiver_ahead()` (`git_repo_sync.py`) skips in `_auto_apply_decision` with
> `receiver is ahead of incoming … re-snapshot the producer`, and `apply_repo()` re-checks
> **post-fetch** (`merge-base --is-ancestor <recv_head> <meta.head>`), which also catches
> same-branch true divergence the pre-fetch heuristic can't see. Manual `git apply` skips
> with the same message unless `--force` (receiver still backed up first, R15). `git status`
> flags such repos `[receiver-ahead (STALE incoming)]`. Tests:
> `tests/test_issue006_receiver_ahead.py`. Fixes A–C above (ff-only return path,
> sticky-skip escalation, profile status) remain open.

`_auto_apply_decision` (`fsync/homesync.py:1107-1130`) refuses on busy / dirty / branch-divergent
receivers, and skips when `recv_head == meta["head"]` ("up to date"). Every other case falls
through to `return True, "clean"`. There is **no check that the producer is actually ahead** —
`meta["head"]` is applied whenever it merely *differs* from the receiver's HEAD.

So a producer whose bundle is stale relative to the receiver will **move the receiver backwards**.
This was live during this reconciliation: fury sat at `458660f` while the staged bundle advertised
`aef11ce` (its parent). The only thing standing between fury and a silent one-commit regression was
an untracked `logs/` directory making `is_dirty` (`git_repo_sync.py:465-470`, which counts
untracked files) return true — the same condition reported for weeks as the benign-sounding
`receiver has local changes`. Delete that stray directory and the next run rolls fury back.

**Closed for now** by re-running `fsync git snapshot` on minis so the bundle advertises `458660f`.

**Fix:** before applying, require `git merge-base --is-ancestor <recv_head> <meta.head>`; if the
receiver is ahead, skip with `receiver is ahead of producer` and raise `status: attention`. This is
one call and turns an accidental save into a guarantee. It also composes with fix **A** above:
once `kind:git` may flow both ways ff-only, "receiver is ahead" becomes the trigger to send it home
rather than a reason to stop.

---

## ISSUE-007 — Deletions never propagate: a file removed on one box lives on the other forever, unreported

- **Status:** OPEN — incident resolved by hand 2026-07-28. Same reconciliation as ISSUE-002..006.
- **Where seen:** `workspaces/primary/asap/trunk` — 13 files, stranded on fury4dx for **14 days**.

### Symptom
On **2026-07-14** twelve stale pre-reorg `trunk/assets/*.json` and a duplicate
`trunk/android/app/src/main/kotlin/com/example/flutterasap/MainActivity.kt` were moved out of the
working copy into `asap/scratchpad/trunk-stale-2026-07-14/`. That cleanup was performed on
**minis4dx**. On **2026-07-28** all thirteen were still sitting in fury4dx's `trunk/`, byte-identical
to the quarantined copies, while minis' `trunk/` had been clean the whole time.

Nothing ever reported this. The `primary` profile logged `status: ok` on every run for two weeks.
The files are untracked in SVN, so no SCM surfaced them either. They were found only by an explicit
`rsync -c` sweep, and identified as junk only because a human remembered the 2026-07-14 cleanup.

### Root cause (verified against code, 2026-07-28)
Deletions are **structurally impossible** in a profile run — not merely disabled:

1. `Profile` (`fsync/homesync.py:330-349`) has **no `mirror` field**. It carries `direction`,
   `conflict`, `kind`, `mirror_branches` (a git concern) — nothing that selects a mirror mode.
2. `build_sync_plan` (`fsync/fileindex.py:610`) only ever populates `a_delete` / `b_delete` under
   `mirror='a-to-b'` or `'b-to-a'` (`:696-707`). The profile path always reaches the
   **union** branch (`:708`), where both delete lists are returned empty (`:733-734`).
3. `fsync/homesync.py` never passes `--delete` to rsync for a profile leg, and never reads
   `a_delete`/`b_delete`. (The two `--delete` hits at `:562` and `:1582` are the deploy/venv
   mirroring helpers, not profile sync.)

So every profile leg is purely additive, in both one-way and `direction: both` modes.

**The deeper reason the safe default is safe:** a run compares two *live* trees with no memory of
the previous run. Given "path exists on A, absent on B", **"B deleted it" and "A created it" are
indistinguishable**. fsync always resolves that ambiguity as *created* — which is correct, and is
precisely the guard that keeps the ISSUE-001 ghost incident from being a data-loss incident instead.
The defect is not the choice; it is that the discarded interpretation is never even **reported**, so
a real deletion silently degrades into permanent divergence.

**No baseline is retained.** A run directory holds only `progress.json`, `report.json` and
`git-bundles/` — no index snapshot. Yet the machinery already exists: `fsync index --output` writes
JSON/JSONL and `compare` consumes it (`fileindex.load_index_file`, `iter_index_records`;
`cli.py` even has `--store-db`). Persisting one index per profile path per run is wiring, not new
capability.

### Resolution (the incident)
Removed the 13 files from fury by hand
(`asap/scratchpad/fsync-recon/80-remove-ghosts.sh`), gated by a pre-flight that refused to delete
anything unless it was untracked **and** had a byte-identical copy in the 2026-07-14 quarantine dir
(which still exists on both boxes). Both boxes now agree.

Two traps that pre-flight caught, worth recording:
- `trunk/android/app/src/main/kotlin/**com**` is a **versioned (empty) SVN directory** — a leftover
  of the `net.afsas.asap` package rename. Only `com/example` and below was untracked. Deleting
  `com/` would have left the working copy with a `!` missing entry.
- **Plain `svn status` prints nothing for ignored paths**, so an ignored file is indistinguishable
  from a versioned-and-clean one. `svn info` is the authoritative test — it errors for anything not
  under version control.

### Fix (proposed, 0.6.0)
- **A. Persist a per-run index** per profile path (`~/.local/state/fsync/runs/<id>/index-<profile>.jsonl`,
  or a rolling `last-index` per profile). Reuses `index --output` / `load_index_file` as-is.
- **B. Detect and REPORT, do not delete.** With a baseline, "present in baseline **and** still on A
  **and** now gone from B" is an unambiguous deletion on B. Surface it as `deleted_on_peer` in the
  report and raise `status: attention` (see ISSUE-003). Report-only is exactly how ISSUE-001 fix A
  shipped `untracked_introductions` — the established pattern here, and it alone would have surfaced
  these 13 files on **2026-07-15**.
- **C. Only then, optional propagation** behind an explicit per-profile `propagate_deletes: true`
  (default off), backing up every removed file to `backup_root` first, as the transfer legs already do.
- **D. Absent or stale baseline must mean "do nothing".** No baseline (first run, upgrade, cleared
  state) or one older than N days ⇒ report only, never delete. The failure mode to design against is
  a missing baseline being read as "everything was deleted".
- **E. Order matters: land ISSUE-005 first.** While `.dart_tool`, `.flutter-sdk`, `build/`, `bin/`
  and `*.class` are still in scope, ~99% of candidate paths are regenerable artifacts that appear and
  vanish per build. Delete-propagation over that set would be both deafening and dangerous.

### Prevention
An additive-only sync is a reasonable default, but "additive" silently means **divergence is
permanent and unbounded**: every deletion either happens twice by hand or never happens at all.
Detection is cheap and carries no risk — the ambiguity fsync cannot resolve is still worth showing
the human who can.

---

## ISSUE-001 — Cross-box "ghost": a file tracked on one branch reappears *untracked* on the peer

- **Status:** incident **RESOLVED** 2026-07-25 (both boxes aligned to `main @ 965a7e4`).
  fsync product fix: **A + B in 0.4.0**; **C (C-b) in 0.5.0**, and **ACTIVATED 2026-07-25** — the
  `repos` profile is now `apply: auto` on both boxes (dry-run verified safe). Fully closed.
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
- **C. File profiles skip git worktrees + guarded receiver auto-apply — DONE (0.5.0, C-b).**
  A `kind:git` profile gains `apply: on-demand | auto` (default `on-demand`). With `apply: auto`:
  (1) `git_repo_excludes_for_file_profiles` injects per-repo subtree excludes so file profiles no
  longer double-carry those repos; (2) the receiver auto-applies bundles in a post-pass of
  `sync run` via `auto_apply_git_profile`, guarded by `_auto_apply_decision` — applies only a new,
  or a not-busy + clean + same-branch repo; else skips with a reason. Every real apply backs the
  receiver up first (R15); `--dry-run` mutates nothing. **Inert until opted in** — the code ships in
  0.5.0 but changes nothing until the `repos` profile sets `apply: auto`.
  **ACTIVATED 2026-07-25:** `repos` profile set to `apply: auto`, deployed to both boxes. Dry-run on
  fury confirmed safe: file profiles now exclude 13 (homelab) + 15 (primary) repo trees, and
  auto-apply = 0 applied / 27 skipped / 0 errors — every repo either "up to date" (no churn) or
  protected as "receiver has local changes" (guard refuses to clobber). NOTE: several fury repos are
  legitimately dirty (e.g. dashboard-refactor-v4 = active work) and stay frozen until committed/
  reverted on fury — auto-apply resumes once clean. Fix A's report surfaces any leftover untracked
  ghosts to clean up per-repo.

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

**Decisions (resolved 2026-07-25):** C-b chosen. Safe policy = apply only when the receiver repo is
new, or not-busy + clean + same-branch; else skip + report. Config surface = per-`kind:git`-profile
`apply: on-demand | auto`, default `on-demand`.

**Status:** implemented in 0.5.0 (see the Fix/C bullet above), tests in
`tests/test_issue001_autoapply.py`. **Remaining = the config flip:** set `apply: auto` on the `repos`
profile once validated with a `--dry-run`; this is a live behavioural change (fury's repos start
auto-applying) so do it deliberately, not by default.
