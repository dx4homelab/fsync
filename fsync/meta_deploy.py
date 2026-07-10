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


# --------------------------------------------------------------------------- #
# deploy targets from a multi-host config                                     #
# --------------------------------------------------------------------------- #

def _match_host_key(hosts: dict, hostname: str) -> str | None:
    if hostname in hosts:
        return hostname
    short = hostname.split(".")[0]
    return short if short in hosts else None


def remote_targets(data: dict, self_host: str) -> list[tuple[str, str]]:
    """(host_key, ssh_target) for every host except self. ssh_target comes from
    `hosts[R].ssh`; for a 2-box config with no `ssh:`, it falls back to self's
    `peer` (self's peer *is* the remote)."""
    hosts = data.get("hosts") or {}
    self_key = _match_host_key(hosts, self_host)
    out: list[tuple[str, str]] = []
    for hk, hcfg in hosts.items():
        if hk == self_key:
            continue
        ssh = (hcfg or {}).get("ssh")
        if not ssh and self_key and len(hosts) == 2:
            peer = (hosts.get(self_key) or {}).get("peer") or {}
            if peer.get("host"):
                ssh = (f"{peer['user']}@" if peer.get("user") else "") + peer["host"]
        if not ssh:
            raise DeployError(
                f"host '{hk}' has no ssh: address (needed to deploy to it)")
        out.append((hk, ssh))
    return out
