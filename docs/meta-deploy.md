# Meta-deploy — requirements & design draft (v1, 2026-07-10)

## Context

Meta-sync (P5) keeps the *config* coherent across boxes via `include:` + a
`requires:` gate, and relies on fsync being an **editable install** so the code
propagates by syncing the repo. That means fsync's source is **dual-hosted** —
a full git checkout + venv on every box — which is fragile (branch state drift,
the tool syncing its own working tree) and is exactly what we just excluded from
git-sync.

P6 replaces dual-hosting with **build-and-push deployment**:

1. **Single source of truth.** fsync's source lives on **minis4dx only**.
   Remotes run a built artifact, not a checkout.
2. **A self-contained `.pyz`** (Python zipapp) bundling the fsync **core** + a
   single **multi-host config**, where `socket.gethostname()` selects the active
   profile set (peer, direction) at runtime — one artifact, one config.
3. **`fsync deploy`** pushes the pyz + config to each remote over SSH,
   generalizing the existing `ensure_peer_engine` (which already rsyncs an engine
   copy to the peer for remote indexing).

## Requirements

- **R20 — Core-only, dependency-free artifact.** The `.pyz` bundles only the
  headless core (`index`, `compare`, `sync`, `git`, `meta`, `deploy`,
  `sync-plan`) + a vendored pure-Python **PyYAML**. It runs on any box with a
  bare `python3` — no venv, no pip, no repo. The UI surfaces (`tui`/`web`/`gtk`/
  `daemon`, which need native deps — pydantic-core, PyGObject, textual) are **not
  in the pyz**; they stay a pip/editable install on boxes you sit at.
- **R21 — Hostname-selected multi-host config.** One config file describes all
  hosts; each box self-selects its `peer`/`direction` by hostname. Box-local
  identity is data, not per-box files.
- **R22 — Compatibility gate preserved.** The multi-host config keeps
  `requires:` (P5); a box refuses config needing capabilities its build lacks.
- **R23 — Deploy over the existing SSH trust.** `fsync deploy` uses the same
  box-to-box SSH (BatchMode, pinned host key) as the rest of fsync — no new
  credential path, no external service.
- **R24 — Atomic, reversible install.** Each remote's fsync binary + config are
  written to a temp path and `mv`'d into place; the previous binary + config are
  backed up first. A half-finished push never leaves a broken `fsync`.
- **R25 — Verifiable.** After a push, deploy runs `fsync meta version` on the
  remote and confirms the expected version + that the remote self-selected the
  right host block. `fsync deploy status` reports each remote's deployed version
  and drift without pushing.
- **R26 — Source stays minis-only, safely.** Nothing in deploy requires the
  remote to hold fsync source; deploying does not touch the remote's repo. Once
  a remote runs the pyz, its `workspaces/homelab/fsync` checkout can be removed.

### Non-goals (v1)

- Bundling native-dep UI surfaces in the pyz (they stay pip/editable).
- Embedding the config *inside* the pyz (config is pushed as a sibling file so it
  can change without a rebuild; a baked-in fallback is a Roadmap item).
- Cross-platform / cross-Python pyz (both boxes are Fedora Atomic x86_64, same
  `python3`; the core is pure-Python so this mostly doesn't matter, but PyYAML's
  optional C extension is intentionally omitted — the pure-Python loader is used).
- Multi-box (>2) topologies get a per-host `ssh:` address; the 2-box case derives
  it from `peer`.

## Multi-host config

One file (`fsync-hosts.yaml`), deployed identically to every box; each box reads
it and self-selects by hostname. Supersedes the P5 `include:` split.

```yaml
version: 1
requires: [git-sync]                     # P5 compatibility gate (top-level)
hosts:
  fury4dx:
    ssh: developer@fury4dx.lan           # how OTHER boxes reach this one (deploy target)
    peer: {host: minis4dx.lan, user: developer, home: /var/home/developer}
    defaults: {direction: push}          # box-local override of shared.defaults
  minis4dx:
    ssh: developer@minis4dx.lan
    peer: {host: fury4dx.lan, user: developer, home: /var/home/developer}
    defaults: {direction: pull}
shared:                                  # everything identical across boxes
  defaults: {conflict: newer, workers: 8, backup_root: ~/.fsync/backups}
  profiles:
    repos:        {kind: git, paths: [workspaces/primary, workspaces/homelab], exclude: [...]}
    git-bundles:  {paths: ['.fsync/git-bundles']}
    documents:    {paths: [Documents, Pictures, Desktop, eclipse-workspace]}
    primary:      {paths: [workspaces/primary], exclude: ['.git', '.venv', ...]}
    homelab:      {paths: [workspaces/homelab], exclude: ['.git', '.venv', ...]}
    # ... tools, secrets, dotfiles, claude
```

**Resolution** (in `load_config`): a config with a top-level `hosts:` key is
multi-host. Pick `hosts[H]` where `H` matches `socket.gethostname()` (exact, else
the label before the first dot). The active config is
`deep_merge(shared, hosts[H])` with `requires` carried from the top — yielding the
same `(peer, profiles, defaults)` the single-host format produces. Unknown
hostname → a clear error listing the configured hosts. Single-host / `include:`
configs still parse unchanged (backward compatible).

## The pyz

Built with the stdlib `zipapp`. A staging dir is assembled with the **core**
fsync modules + a vendored pure-Python `yaml/`, then archived with interpreter
`/usr/bin/env python3` and entry point `fsync.cli:main`:

```
stage/
  fsync/        __init__, cli, fileindex, homesync, git_repo_sync, meta_deploy,
                db, catalog_client, eventbus, agent   (lazy-imported; deps optional)
  yaml/         PyYAML pure-Python package (no _yaml.so)
→ zipapp.create_archive(stage, "fsync.pyz",
      interpreter="/usr/bin/env python3", main="fsync.cli:main")
```

UI modules (`tui`, `web`, `views`, `daemon`, `gtk_app`, `client`) are **omitted**;
`cli.py` imports them lazily, so `fsync web`/`gtk`/`tui`/`daemon` on the pyz fail
with a clear "not in this build" message while the core commands work. The result
is one executable file (`chmod +x fsync.pyz; ./fsync.pyz meta version`).

## `fsync deploy`

- **`fsync deploy build [--config F] [--out P]`** — assemble + zipapp the pyz to
  `P` (default `~/.fsync/deploy/fsync.pyz`). Prints size + bundled modules.
- **`fsync deploy push [--config F] [--host N] [--dry-run]`** — build, then for
  each host in `hosts:` except self: rsync the pyz → `~/.local/bin/fsync.tmp` and
  the config → `~/.config/fsync/fsync-hosts.yaml.tmp` over SSH; back up the
  remote's current binary+config; `chmod +x` + atomic `mv` both into place;
  verify with `ssh <remote> fsync meta version`. `--dry-run` prints the plan
  (targets, files, sizes) and does nothing.
- **`fsync deploy status [--config F]`** — for each remote, `ssh fsync meta
  version`; report deployed version + features + whether it matches local and
  self-selected the right host.

**Remote target** = `hosts[R].ssh`; for a 2-box config it falls back to
`hosts[self].peer` (self's peer *is* the remote). **Auth/transport** = the
existing `_ssh` (BatchMode, StrictHostKeyChecking) + `rsync -e ssh`. **Atomicity**
(R24): temp + `mv`, prior binary+config copied to `*.bak` first. **Self is never
a target** — minis keeps its editable dev install.

## Testing

- **Config resolution:** a `hosts:` config selects the right `peer`/`direction`
  per (monkeypatched) hostname; unknown host errors; `requires` gate still fires;
  single-host + `include:` still parse.
- **pyz build:** `build_pyz` produces a runnable archive; `python3 fsync.pyz meta
  version` (subprocess) prints the right version/features and self-selects the
  host; the archive omits the UI modules and contains `yaml/`.
- **deploy plan:** `deploy push --dry-run` lists the correct remote target(s) and
  files; the push path is unit-tested with `_ssh`/rsync monkeypatched (temp→mv
  sequence, backup-first, verify call).
- **scratchpad:** build a real pyz on minis and run it end-to-end; simulate a push
  to a throwaway `$HOME` "remote" over `ssh localhost` (or a local install dir) to
  prove the temp→backup→mv→verify flow.

## Phased plan

- **P6.1 — multi-host config resolution** (`load_config` `hosts:` support) + tests.
- **P6.2 — pyz build** (`meta_deploy.build_pyz`) + scratchpad proof it runs headless.
- **P6.3 — `fsync deploy` build/push/status** + tests (dry-run + monkeypatched push).
- **P6.4 — cutover (deploy step, user-run):** author `fsync-hosts.yaml` from the
  current shared config, `fsync deploy push` to fury, verify, then retire fury's
  fsync source checkout (source becomes minis-only per R26).

## Roadmap (out of v1)

- Config baked into the pyz as an importlib.resources fallback (self-contained
  first-run), overridden by an on-disk config.
- `fsync deploy status` health/heartbeat + auto-rollback on failed verify.
- Signing the pyz; content-hash version stamping.
