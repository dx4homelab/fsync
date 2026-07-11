"""Meta-deploy (P6): build a self-contained fsync .pyz and compute deploy
targets from a multi-host config. Design: docs/meta-deploy.md.

The pyz bundles the headless CORE (index/compare/sync/git/meta/deploy) + a
vendored pure-Python PyYAML, so a remote runs fsync from a bare `python3` with no
venv, pip, or repo. UI surfaces (tui/web/gtk/daemon) need native deps and are
intentionally omitted — cli.py imports them lazily, so they fail with a clear
message on the pyz while the core commands work.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipapp
from pathlib import Path

# Headless core only. UI modules (tui/web/views/daemon/gtk_app/client) are left
# out — they pull native deps and remotes don't run UIs. db/catalog_client/
# eventbus/agent are lazy-imported; included so a remote scanner can still use
# `index --store-db` / `agent` if its deps happen to be present.
CORE_MODULES = [
    "__init__.py", "cli.py", "fileindex.py", "homesync.py", "git_repo_sync.py",
    "meta_deploy.py", "db.py", "catalog_client.py", "eventbus.py", "agent.py",
]

DEFAULT_PYZ = "~/.fsync/deploy/fsync.pyz"
REMOTE_BIN_REL = ".local/bin/fsync"                 # relative to remote $HOME
REMOTE_CONFIG_REL = ".config/fsync/sync-profiles.yaml"  # the pyz's default read path
WHEELHOUSE_REL = ".fsync/deploy/wheelhouse"         # fsync wheel + all deps, per box
DAEMON_VENV_REL = ".fsync/daemon-venv"              # standalone fsyncd venv, per box


class DeployError(RuntimeError):
    """A meta-deploy build/push failed."""


def _pkg_dir() -> Path:
    return Path(__file__).resolve().parent


def _vendor_yaml(stage: Path) -> None:
    """Copy the pure-Python PyYAML package into the stage (omit the _yaml C
    extension — safe_load uses the pure-Python loader)."""
    import yaml
    src = Path(yaml.__file__).resolve().parent
    shutil.copytree(src, stage / "yaml",
                    ignore=shutil.ignore_patterns("__pycache__", "*.so", "*.pyd"))


def build_pyz(out_path: str | Path = DEFAULT_PYZ, *, modules: list[str] | None = None) -> dict:
    """Assemble the core fsync package + vendored yaml and zipapp them into one
    executable .pyz. Returns {path, bytes, modules}."""
    out = Path(os.path.expanduser(str(out_path)))
    pkg = _pkg_dir()
    if not pkg.is_dir():
        # Running from inside the pyz: package files live in the zip and can't be
        # copied out. Build is a source-of-truth (editable install) operation.
        raise DeployError("build must run from a source install, not the pyz "
                          "(the pyz can't unpack itself) — build on the source box")
    out.parent.mkdir(parents=True, exist_ok=True)
    mods = modules or CORE_MODULES
    included: list[str] = []
    with tempfile.TemporaryDirectory(prefix="fsync-pyz-") as td:
        stage = Path(td)
        (stage / "fsync").mkdir()
        for m in mods:
            src = pkg / m
            if src.exists():
                shutil.copy2(src, stage / "fsync" / m)
                included.append(m)
        _vendor_yaml(stage)
        tmp = out.with_suffix(out.suffix + ".tmp")
        zipapp.create_archive(str(stage), str(tmp),
                              interpreter="/usr/bin/env python3", main="fsync.cli:main")
        os.chmod(tmp, 0o755)
        os.replace(tmp, out)
    return {"path": str(out), "bytes": out.stat().st_size, "modules": included}


def build_wheelhouse(out_dir: str | Path | None = None) -> dict:
    """pip-wheel the source checkout + every dependency into a wheelhouse dir
    (cleared first, so retired deps don't linger). Source-of-truth operation
    like build_pyz: the fsyncd venvs on every box install offline from this.
    Native wheels (pydantic-core, pyyaml) are fetched for THIS interpreter —
    the fleet shares one python version, which `deploy daemon` relies on."""
    pkg = _pkg_dir()
    repo = pkg.parent
    if not pkg.is_dir() or not (repo / "pyproject.toml").exists():
        raise DeployError("wheelhouse build must run from the source checkout, "
                          "not the pyz — build on the source box")
    out = Path(os.path.expanduser(str(out_dir or "~/" + WHEELHOUSE_REL)))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(repo), "-w", str(out), "--quiet"],
        capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise DeployError(f"pip wheel failed: {proc.stderr.strip()[-400:]}")
    wheels = sorted(p.name for p in out.glob("*.whl"))
    if not any(w.startswith("fsync-") for w in wheels):
        raise DeployError("wheelhouse built but contains no fsync wheel")
    return {"path": str(out), "wheels": wheels}


# --------------------------------------------------------------------------- #
# deploy targets from a multi-host config                                     #
# --------------------------------------------------------------------------- #

def _match_host_key(hosts: dict, hostname: str) -> str | None:
    """Symmetric short-name matching: gethostname() may be short while the
    config keys are FQDNs, or vice versa — both directions must identify self,
    else self could be treated as a deploy target and clobbered."""
    if hostname in hosts:
        return hostname
    short = hostname.split(".")[0]
    if short in hosts:
        return short
    for hk in hosts:
        if hk.split(".")[0] == short:
            return hk
    return None


def remote_targets(data: dict, self_host: str) -> list[tuple[str, str]]:
    """(host_key, ssh_target) for every host except self. ssh_target comes from
    `hosts[R].ssh`; for a 2-box config with no `ssh:`, it falls back to self's
    `peer` (self's peer *is* the remote). Refuses to run when self can't be
    identified, and refuses any target that resolves back to self — deploying to
    the source box would replace its editable install with the pyz."""
    hosts = data.get("hosts") or {}
    self_key = _match_host_key(hosts, self_host)
    if hosts and self_key is None:
        raise DeployError(
            f"this box '{self_host}' is not in the config hosts {sorted(hosts)} — "
            f"refusing to deploy (cannot tell self from remotes)")
    self_short = self_host.split(".")[0]
    out: list[tuple[str, str]] = []
    for hk, hcfg in hosts.items():
        if hk == self_key:
            continue
        ssh = (hcfg or {}).get("ssh")
        if not ssh and len(hosts) == 2:
            peer = (hosts.get(self_key) or {}).get("peer") or {}
            if peer.get("host"):
                ssh = (f"{peer['user']}@" if peer.get("user") else "") + peer["host"]
        if not ssh:
            raise DeployError(
                f"host '{hk}' has no ssh: address (needed to deploy to it)")
        if ssh.split("@")[-1].split(".")[0] == self_short:
            raise DeployError(
                f"host '{hk}' resolves to this box ({ssh}) — refusing self-deploy "
                f"(check the peer/ssh addresses in the config)")
        out.append((hk, ssh))
    return out
