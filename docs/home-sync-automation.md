# Home-sync automation — requirements & design draft (v1, 2026-07-03)

## Context

Two machines, one owner, multiple roles (architect / developer / devops / platform /
release engineer for a Gov Client):

- **minis4dx** — stationary workstation, always on the home LAN.
- **fury4dx** — laptop, currently the primary machine; occasionally travels to the
  client site, i.e. it is sometimes absent for days and returns "newer".

Both are NVMe clones whose identities have been split (hostname, machine-id, SSH host
keys). The first manual sync was executed 2026-07-03 with `fsync sync-plan` per
subtree over the 2.5G LAN (see git history / project memory). This doc turns that
manual run into an automated, repeatable process.

## Requirements

- **R1 — Bidirectional carry-set sync.** Sync a configured set of `$HOME` subtrees
  between the two boxes, both directions in one run.
- **R2 — Terminal UI first.** The planning/automation UI starts as a TUI
  (`fsync tui`). A web UI (dream4ui page) may come later; nothing in the engine may
  assume a specific UI.
- **R3 — No per-step approval.** A streamlined profile runs end-to-end (plan →
  transfer → report) with zero interactive prompts. Manual approval per step is
  explicitly rejected as too tedious.
- **R4 — Conflict safety via backup folders, not via blocking.** When both sides
  changed the same path, the sync auto-resolves (default: newest mtime wins) and the
  losing version is moved into a dedicated backup folder on the box that got
  overwritten. Nothing is ever silently destroyed; every auto-decision is reversible
  after the fact.
- **R5 — Claude artifacts get an involved pipeline; everything else is streamlined.**
  `~/.claude` keeps conflict-review semantics: credential/identity files never sync,
  per-project memories are merged (superset-check first), remaining conflicts are
  surfaced for review — assisted, but not fully automatic. All other profiles run
  hands-off per R3/R4.
- **R6 — Opportunistic and away-tolerant.** A run only starts when the peer is
  reachable on the trusted home LAN (pinned SSH host key, LAN address). When the
  laptop is at the client site the run is a clean no-op that says so. Days-long gaps
  must be handled (that is the normal laptop pattern, not an edge case).
- **R7 — Auditable runs.** Every run leaves a machine-readable report: what moved in
  each direction, what was backed up where, what is pending review, bytes/duration.
  Reports feed the TUI history view (and later dream4events).
- **R8 — Fast repeated runs.** Hashing happens locally on each box (index exchange
  over SSH — never remote-content-over-sftp) and is incremental: unchanged
  size+mtime ⇒ reuse cached hash. A no-change nightly run should take seconds.

## Engine gaps to close (all hit during the 2026-07-03 manual run)

| Gap | Today | Needed |
|---|---|---|
| Excludes | none — forced per-subtree plans | include/exclude patterns per profile |
| Remote side | sshfs/rclone mount, full content over wire | `fsync index` runs on the peer via SSH, only index JSON crosses the LAN |
| Compare input | two live directories only | compare accepts two saved index files |
| Re-hash cost | full re-hash every run | per-box index cache keyed by (path, size, mtime) |
| Vanished files | rsync exit 23 aborts run.sh (`set -e`) | tolerate vanish (23/24) on live trees, log and continue |
| Renames | hash-match pairs trivial duplicates (empty `.lock` files) across unrelated dirs | suppress rename proposals when same hash occurs at >2 paths or size below floor |
| Conflict handling | review = held, nothing moves | `newer` + `--backup --backup-dir` per R4 |

## Proposed design

### Profiles (`~/.config/fsync/sync-profiles.yaml`)

```yaml
peer:
  host: minis4dx            # pinned host key; LAN address only
  user: developer
defaults:
  mode: bidir
  conflict: newer           # loser goes to backup_dir
  backup_dir: ~/.fsync/backups/{run_id}/{profile}
  retention: 60d
profiles:
  documents:  { paths: [Documents, Pictures, Desktop, eclipse-workspace] }
  tools:      { paths: [tools] }
  secrets:    { paths: [secrets] }
  dotfiles:   { paths: ["."], recursive: false, exclude: ["*.tmp", ".bash_history*"] }
  claude:
    paths: [.claude]
    conflict: review        # R5: involved pipeline
    exclude: [".credentials.json", "backups/*"]
    merge_jsonl: ["history.jsonl"]   # line-union, timestamp-interleaved:
        # append-only JSONL diverges whenever both boxes are used, and
        # whole-file resolution can only clobber a side. Matching files are
        # deduped+merged (old copies backed up both sides), pushed when
        # direction allows, and never held as conflicts. SHIPPED 2026-07-04.
```

### Backup mechanics (R4)

Receiving side of every transfer runs rsync with
`--backup --backup-dir=<backup_dir>` so an overwritten file's previous version
lands under `~/.fsync/backups/<run-id>/<profile>/<relative-path>`. The TUI's
conflict browser lists backups per run with one-key restore. A retention job
prunes expired runs.

### Run flow (headless core, R3/R6/R8)

```
fsync sync run [--profile X | --all] [--headless]
  1. peer reachable + host key matches?  no → exit 0 "peer away"
  2. both sides: fsync index (local, incremental) → exchange index JSON over ssh
  3. compare indexes per profile → plan (excludes applied)
  4. execute: additive both ways + newer-wins with backup-dir; claude profile
     executes additive legs only, holds conflicts
  5. write report JSON + human summary; nonzero exit only on real errors
```

### TUI (`fsync tui`, R2)

Textual app, four screens:
1. **Profiles** — carry set, per-profile policy, last-run status, peer state.
2. **Run** — trigger one/all, live progress (files, MB/s, per-direction counts).
3. **Review** — claude-profile conflicts + memory-merge assistant; batch actions
   (take-A / take-B / skip) instead of per-file prompts.
4. **History/Backups** — past run reports, backup browser, restore.

### Phasing

- **P1 — engine:** close the gap table, add profiles + `fsync sync run --headless`
  with backup-dir semantics. (Automation exists from P1 on: one command, no UI.)
  **SHIPPED 2026-07-03**: excludes + index-file compare + per-root hash cache in
  `fileindex.py`; rename suppression (duplicate-content demotion) in
  `build_sync_plan`; `fsync/homesync.py` (profiles, SSH engine push + remote
  index, backup-dir executor, run reports under `~/.local/state/fsync/runs/`);
  `fsync sync init|run` CLI; tests in `tests/test_homesync_p1.py`. A no-change
  `run --all` (8 profile paths, ~5,800 files verified both boxes) takes ~6s.
- **P2 — TUI:** the four screens above on top of run reports.
  **SHIPPED 2026-07-03** (`fsync tui`, fsync/tui.py, Textual): plan preview
  ("what is coming": per-path push/pull counts+bytes, overwrites, conflicts) →
  ONE confirmation (`r`; `d` toggles dry-run) → the run executes in a
  **detached** process (`start_new_session`) that survives the UI; the UI
  polls `<run_dir>/progress.json` (atomic snapshots from ProgressWriter, incl.
  streamed rsync bytes/%/files via --info=progress2) and **re-attaches on
  restart** via `~/.local/state/fsync/current-run.json` (pid-liveness check —
  a dead runner with stale "running" renders as aborted). Engine additions:
  `fsync sync run --plan-only` (JSON preview, no lock), shared `_plan_path`
  so preview and execution can't diverge semantically. Proven headlessly via
  textual run_test: plan → confirm → kill UI mid-run → restart → re-attach →
  finished (scratchpad/p2_tui_drive.py, scratchpad/p2_detach_test.py).
- **P3 — hands-off:** systemd user timer (peer-reachable gated), notifications,
  optional `claude -p` triage for the claude profile's residual conflicts.
  Note: headless `fsync sync run` remains promptless — the P2 confirmation
  lives only in the TUI, so the timer path is unaffected.
  **SHIPPED 2026-07-03**:
  - `fsync sync timer install|remove|status` writes/enables systemd user
    units (`fsync-sync.timer`: OnBootSec=3min, OnUnitInactiveSec=1h default
    via `--interval`, RandomizedDelaySec=4min). SSH works agentless (key on
    disk), so the service needs no agent plumbing; notify-send gets the
    session bus via `DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus`.
    **Gotcha fixed 2026-07-03:** the first units used OnUnitActiveSec, but a
    Type=oneshot service may never latch "active" — the monotonic timer then
    has no reference point and silently stops rescheduling (observed NEXT=-
    after a firing). OnUnitInactiveSec (next = interval after the previous
    run finished) is always well-defined. Relatedly, a local-lock overlap
    exits 0 so a timer firing during a manual/TUI run never lands the
    service in the failed state.
  - **Deployed on BOTH boxes** (fury4dx 2026-07-03 afternoon, minis4dx
    evening with the fixed units). `loginctl enable-linger developer` set on
    minis4dx so its timer survives logout/reboot-without-login. The
    randomized delay staggers the two schedules; if they ever coincide, the
    cross-box lock defers one — either box syncing converges the pair, so
    dual timers are redundancy, not conflict.
  - The TUI's always-visible status strip (runner ●/○ + pid/elapsed, next
    timer firing via `list-timers --output=json` — the `show` NextElapse
    property is empty for monotonic timers — and last-run moved/held/error
    stats) is how you see background activity at a glance; `fsync sync
    timer status` prints the same one-liner headlessly.
  - **Cross-box lock** (both boxes are drivers): before transferring, the
    runner probes the peer's `~/.local/state/fsync/sync.lock` with
    `flock -n` over SSH and defers cleanly (exit 0) if held; flock(1) and
    Python fcntl share BSD semantics so the probe is exact. Simultaneous
    starts both back off — safe, next timer retries.
  - `--notify`: desktop notification only when files moved or the run
    failed; standing held conflicts alone stay silent (no hourly spam).
  - Triage helper `scratchpad/triage_claude_conflicts.sh`: pipes the latest
    run's held-conflict JSONs to `claude -p` for take-a/take-b/merge/
    leave-per-machine recommendations. Advisory only.
  - Verified live: timer-triggered service run (6.1s wall, 32M peak, pulled
    1 file, notification fired); peer-lock defer + resume both proven.

### P1 field findings (2026-07-03)

- **Claude Code GC vs sync (important):** `~/.claude` session ephemera
  (transcripts `projects/*.jsonl`, `file-history/`, `todos/`, `plans/`, caches)
  are pruned by Claude Code's own ~30-day cleanup. Syncing them ping-pongs
  forever: the first live run pulled 3,443 such files from the peer, of which
  3,440 were older than 30 days — the receiver's next GC (observed via
  `.last-cleanup`) deletes them and the next sync re-copies them. The claude
  profile therefore syncs **durable artifacts only** (project `memory/`,
  agents, skills, plugins; settings held as review conflicts) and excludes all
  GC-managed dirs. Steady state: 0 transfers, 13 per-machine conflicts held.
- **Name resolution:** bare `minis4dx` (LLMNR) stopped resolving after a peer
  reboot; the router's DNS name `minis4dx.lan` is the reliable target and is
  now the template default. `peer_reachable()` treats resolution failure as
  "peer away" (clean no-op) — correct behavior, but check the name if runs
  keep no-opping unexpectedly.

## P4 — engine/UI split + multi-UI (requirements recorded 2026-07-04)

### Requirements

- **R9 — UI/engine separation.** UI code and operational code live apart. No
  UI imports engine internals; every operation and observation goes through
  the backend's API. The engine keeps working with no UI attached.
- **R10 — REST backend, TLS-first.** The operational backend (`fsyncd`) is a
  persistent per-box daemon (systemd user service) exposing a web/REST API:
  plain TLS on localhost, **mTLS for any non-local client**. Self-signed
  certificates to start (pinned peer-style, like the ssh host keys); scoped
  to the LAN pair for now — off-LAN reach is a later tunnel/cert iteration.
  The daemon absorbs scheduling (the systemd timer becomes a thin POST or
  retires in favor of daemon-internal scheduling) and serves: profiles,
  plan/preview, run trigger + live progress, reports/history, held
  conflicts, backup browsing/restore, peer status.
- **R11 — pluggable UI family.** All thin clients of R10's API:
  (a) **TUI** — the existing Textual app refactored into an API client;
  (b) **local web** — browser UI served locally;
  (c) **native Linux GUI** — Python GTK, with **always-on-top** window
  support (a compact sync status/control panel that floats over work).
- **R12 — dream4ui-lite.** The UI layer is built on a NEW feature branch in
  the dream4ui framework (dream4devops repo): a limited/basic component set
  (status strip, table, progress, action buttons, conflict list) defined as
  data (YAML/Pydantic, per dream4devops house style) with per-target
  renderers (Textual / web / GTK). Explicitly **not required to be
  compatible** with the rest of dream4ui.

### P4.1 SHIPPED + DEPLOYED 2026-07-04

Branch `p4.1-fsyncd`. Engine/UI split delivered and running on both boxes:

- **`fsync/certs.py`** — per-box self-signed EC P-256 keypair (openssl), dual
  serverAuth/clientAuth EKU, `CA:FALSE` (exact-cert pin, not a CA anchor).
  Peers pin each other's cert; a `0600` `api-token` gates loopback callers.
- **`fsync/daemon.py` (fsyncd)** — FastAPI over the engine's on-disk state;
  runs stay detached subprocesses. Two app instances share one PlanJobs/
  Scheduler/run-lock: loopback (127.0.0.1, TLS + token) and LAN (mTLS,
  `CERT_REQUIRED` against pinned peers). Internal scheduler replaced the P3
  timer. `fsync daemon run|install|remove|status|trust|cert`.
- **`fsync/client.py`** — the only module UIs touch; httpx over an explicit
  pinned `SSLContext` (presents the client cert for mTLS — httpx 0.28's
  tuple `cert=` does not). Sends the local token; peers use their cert.
- **`fsync/tui.py`** — pure API client (import-boundary enforced by test);
  async tick runs all HTTP off the event loop; status strip reads the daemon
  and shows the peer's runner over mTLS.

Deployed: fsyncd active on fury4dx + minis4dx, mutual cert trust pinned
(fingerprints cross-verified), both LAN mTLS listeners open, both P3 timers
retired. Verified: local token gate (401 without), exact-cert pin (wrong
cert rejected), cross-box mTLS both directions (200), certless client
rejected, TUI kill/re-attach, clean SIGTERM. An adversarial review (39
agents) found 18 issues; the confirmed set (exact-pin, loopback auth, run
TOCTOU, fast deferrals, scheduler re-anchor, interval floor, 422 bodies,
plan cap, run-dir GC, async TUI, DNS-failure mTLS) was fixed with regression
tests. 69 tests pass.

### P4.2 SHIPPED + DEPLOYED 2026-07-04

**dream4ui-lite** (dream4devops `feature/ui-lite`, `src/dream4devops/ui_lite/`):
a render-target-agnostic UI toolkit — pure-Pydantic `Screen` of Strip/Table/
Bar/Actions/Note (zero UI deps), a Rich renderer (`render_textual`) and an
HTML renderer (`render_web`, stdlib-only, escaped, `Tone`→CSS). One spec
renders to terminal AND browser. Deliberately not compatible with the richer
dream4ui.

**fsync** (`p4.1-fsyncd`): `views.py` builds lite Screens from fsyncd API
data — the single source of truth. `tui.py` rewired to render those via
`render_textual` (all Rich-table code deleted; import boundary holds). `web.py`
+ `fsync web`: a loopback renderer process holding one FsyncClient (browser
needs no credential), rendering the SAME Screens via `render_web`, with a
~30-line vanilla-JS htmx shim served locally (no CDN). View builders return
CONTENT ONLY; each surface places the status strip itself (TUI → pinned
widget, web → `views.with_strip`).

Decisions taken (the user, as architect): shared spec drives BOTH TUI and web
now; separate `fsync web` renderer process; htmx fragment polling.

Adversarial review (27 agents) → 5 confirmed, all fixed: web CSRF +
DNS-rebinding closed by a per-route guard (Host must be loopback; Sec-Fetch-
Site must be same-origin/none); TUI status-strip double-render; web session
races (poll() under lock, screen() snapshots + guards plan==None, prev-run
adoption guard, cached peer client). Tests: 75 fsync + 5 ui_lite pass;
web/TUI e2e green; `fsync web` verified serving + guards on both boxes.

### P4.3 SHIPPED + DEPLOYED 2026-07-04

**`fsync gtk`** — the native GTK panel, the third UI surface. Renders the SAME
dream4ui-lite Screens as the TUI and web through a new backend
(`ui_lite/render_gtk.py`: Screen → GTK3 widgets). Two modes (as the user
specified):

- `fsync gtk` — normal resizable window, full parity (plan toggles, progress,
  actions).
- `fsync gtk --always-on-top` — compact glanceable panel (`views.compact_screen`:
  status strip + progress bar / idle line + Sync-now / Re-plan / dry-run /
  open-full), pinned above other windows.

Always-on-top on GNOME/Wayland (where GTK4 has no such API and mutter has no
layer-shell): a GTK3 window under XWayland sets `_NET_WM_STATE_ABOVE` via
`set_keep_above()`, which mutter honors for X11 clients. The panel forces
`GDK_BACKEND=x11` and re-asserts keep-above on map — verified live (xprop
shows ABOVE). gi is borrowed from system site-packages (same ABI) so it runs
from the venv without a pip build. The GTK app reuses the reviewed WebSession
state machine wholesale; only rendering differs.

Adversarial review (25 agents) → 3 confirmed, all fixed: HIGH GTK UI-freeze
(peer probe moved out of the session lock + act() runs off the GTK main
thread), MEDIUM dead compact "Sync now" outside plan mode (+ a Re-plan
action), LOW indeterminate-bar animation (host pulses on a 90ms timer). 77
fsync + 6 ui_lite tests pass; deployed and GTK-rendering verified on both
boxes.

**All of R11 is now delivered: TUI, local web, and native GTK (always-on-top),
all thin clients of fsyncd rendering one dream4ui-lite spec.** Remaining:
P4.4 (mTLS hardening / off-LAN).

### P4.4 SHIPPED + DEPLOYED 2026-07-04 (off-LAN; completes P4)

Off-LAN reach for the laptop at the client site, designed generically so the
tunnel is swappable (Tailscale recommended):

- **Multi-address peer**: `peer.addresses` is an ordered list (LAN name first,
  tailnet name/IP next). `peer_reachable()` probes each over ssh, pins the
  first reachable on the Peer; the sync (ssh/rsync) and the API both follow it.
- **Exact-cert pin, hostname-check off**: the pinned self-signed cert is the
  identity proof, so TLS hostname matching is dropped — reach the pinned peer
  by any address (tailnet IP, MagicDNS) with no SAN juggling.
- **All-interface mTLS** on its own port (`mtls_port`, default `port+2`=7446;
  loopback token-TLS stays 127.0.0.1:port — 0.0.0.0 would collide). Safe:
  `CERT_REQUIRED` rejects unpinned callers.
- **Cross-box mTLS centralized in the daemon**: `/v1/peer` connects to the
  peer daemon and relays its runner state; UIs just read the local relay (no
  more per-UI cross-box mTLS or hardcoded `.lan`).

Review (29 agents) → 2 fixed. The load-bearing one: the SYNC path is
ssh/rsync, which verifies host keys — `StrictHostKeyChecking=yes` refused a
new off-LAN address, silently no-opping the run (the exact P4.4 case). The
first "raw IP works" check had only exercised the cert-pinned mTLS API, not
ssh. Fix: `accept-new` (pin unknown host first connect over the authenticated
tailnet; still refuse a changed key). Verified properly: a full sync (index +
rsync legs) runs to minis4dx by a fresh unpinned raw IP. Also: peer status
now shows ssh-sync health as primary and surfaces API-up/sync-down
divergence. 81 tests pass; deployed both boxes.

**Off-LAN bring-up (the one manual step)** — both boxes are currently logged
out of Tailscale: run `tailscale up` on each (per the make-unique plan:
`--force-reauth` to clear the cloned node identity), then add the peer's
tailnet name to `peer.addresses`. Everything else is automatic — the first
off-LAN run pins the tailnet host key and syncs.

**P4 is complete: R9 (UI/engine split), R10 (fsyncd TLS/mTLS, off-LAN), R11
(TUI + web + GTK), R12 (dream4ui-lite) all shipped and deployed.**

### Decisions (settled 2026-07-04)

1. Backend lifecycle: **persistent daemon** on each box.
2. mTLS reach: **LAN pair only for now**; design must not preclude off-LAN
   later, but certs/SANs target fury4dx ↔ minis4dx today.
3. Order: **TUI first** — P4.1 split engine + fsyncd (REST/TLS) + TUI as
   client; P4.2 dream4ui-lite + local web; P4.3 GTK always-on-top panel;
   P4.4 mTLS hardening / off-LAN.

### Notes

- `fsyncd` is the natural evolution of the existing control-plane pieces
  (`fsync agent`, catalog_api's FastAPI) — one daemon can eventually carry
  both home-sync and scanner/catalog duties.
- The progress/report snapshot files (progress.json, report.json,
  current-run.json) become the daemon's internal state; the API replaces
  file-polling for UIs (poll or SSE), which also fixes the status strip's
  per-box blindness — a UI could show BOTH boxes' runners via their APIs.

## Decisions (settled 2026-07-03)

1. `secrets/` **joins the streamlined newest-wins set** — backup-dir preservation
   makes auto-resolution acceptable; restore path covers mistakes.
2. **Manual-first triggering**: `fsync sync run --all` / TUI button in P1–P2; the
   systemd timer (and any on-connect trigger) waits for P3, after trusted runs.
