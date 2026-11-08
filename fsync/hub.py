"""Hub role glue (P7.1, docs/hub-sync.md): picks a spoke's control-plane
backend and implements `fsync hub init|status`.

Layout on the hub box (``hub.root``, default ``~/.fsync/hub``):

    <root>/<profile>/tree        canonical tree per file profile (R31)
    <root>/backups/<run>/<prof>  hub-side --backup-dir of overwritten files (R4)
    <root>/state.sqlite          leases + per-spoke last runs (hubstate.py)
    <root>/hub.json              marker written by `fsync hub init`
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import certs
from . import homesync as hs
from .homesync import HomesyncError, Hub
from .hubstate import HubControl, HubState, LocalHubControl

DEFAULT_PORT = 7444


def hub_state_path(hub: Hub) -> Path:
    """The sqlite file — only meaningful on the hub box itself."""
    if not hub.local:
        raise HomesyncError("hub state lives on the hub box; this box is a spoke")
    return Path(hs.hub_root(hub)) / "state.sqlite"


def _mtls_port(config_path: str | None) -> int:
    """The hub daemon's mTLS port from the shared config's ``daemon:`` block
    (same rule as daemon.load_daemon_cfg, without importing fastapi — this runs
    inside the stdlib-only .pyz on the spokes)."""
    try:
        import yaml
        raw = (yaml.safe_load(Path(config_path or hs.DEFAULT_CONFIG).expanduser().read_text())
               or {}).get("daemon") or {}
    except (OSError, ImportError, ValueError):
        raw = {}
    port = int(raw.get("port", DEFAULT_PORT))
    return int(raw.get("mtls_port", port + 2))


def _pinned_hub_cert(hub: Hub) -> Path:
    for name in (hub.host, hub.host.split(".")[0]):
        p = certs.trust_dir() / f"{name}.pem"
        if p.exists():
            return p
    raise HomesyncError(
        f"the hub's certificate is not pinned on this box — run "
        f"`fsync daemon trust {hub.target} --name {hub.host.split('.')[0]}` once "
        f"(and have the hub trust this box's cert the same way)")


def hub_control(hub: Hub, config_path: str | None = None) -> HubControl:
    """Local sqlite on the hub box; pinned-TLS API from a spoke (R37/R40)."""
    if hub.local:
        return LocalHubControl(HubState(hub_state_path(hub)))
    from .hubclient import HubClient, RemoteHubControl

    pinned = _pinned_hub_cert(hub)
    cert, key = certs.ensure_cert()  # our mTLS identity (the hub must have pinned it)
    port = _mtls_port(config_path)
    ordered = [hub.active_host] + [a for a in hub.all_addresses() if a != hub.active_host]
    client = HubClient([f"https://{a}:{port}" for a in ordered], pinned_cert=str(pinned),
                       identity=(str(cert), str(key)))
    return RemoteHubControl(client)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def _load_hub(config_path: str | None) -> tuple[Hub, dict[str, hs.Profile]]:
    peer, profiles, _defaults = hs.load_config(config_path)
    if not isinstance(peer, Hub):
        raise HomesyncError("this config has no hub (version 1, pairwise peer) — "
                            "see docs/hub-sync.md 'Config v2' to declare one")
    return peer, profiles


def hub_init(hub: Hub, profiles: dict[str, hs.Profile]) -> dict[str, Any]:
    """Create the hub tree + state on this box (idempotent)."""
    if not hub.local:
        raise HomesyncError(f"`fsync hub init` runs on the hub box ({hub.host}); this box is a spoke")
    root = Path(hs.hub_root(hub))
    file_profiles = [p for p in profiles.values() if p.kind == "files"]
    created: list[str] = []
    for prof in file_profiles:
        tree = root / prof.name / "tree"
        if not tree.exists():
            created.append(prof.name)
        tree.mkdir(parents=True, exist_ok=True)
    (root / "backups").mkdir(parents=True, exist_ok=True)
    state = HubState(root / "state.sqlite")
    state.close()
    marker = root / "hub.json"
    info = {"host": hub.host, "root": str(root), "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "profiles": sorted(p.name for p in file_profiles), "fsync": hs.FSYNC_VERSION}
    if marker.exists():
        try:
            info["created"] = json.loads(marker.read_text()).get("created", info["created"])
        except ValueError:
            pass
    marker.write_text(json.dumps(info, indent=2) + "\n")
    return {**info, "new_trees": created}


def _age(seconds: float | int | None) -> str:
    if seconds is None:
        return "never"
    s = int(seconds)
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def render_status(hub: Hub, st: dict[str, Any], profiles: dict[str, hs.Profile]) -> str:
    """Human view of `fsync hub status`: one line per file profile — who holds
    its lease, and each spoke's last run (age, outcome, files up/down)."""
    now = st.get("now") or time.time()
    lines = [f"{hub.label}  root {hub.root}"]
    names = sorted(set(p.name for p in profiles.values() if p.kind == "files")
                   | set(st.get("profiles") or {}))
    width = max([len(n) for n in names] + [7])
    lines.append(f"{'profile':<{width}}  {'lease':<28}  spokes (last run)")
    for name in names:
        row = (st.get("profiles") or {}).get(name) or {}
        lease = row.get("lease")
        lease_s = (f"{lease['host']} ({int(lease['expires'] - now)}s left)" if lease else "—")
        spokes = row.get("spokes") or {}
        if not spokes:
            spokes_s = "no spoke has synced this yet"
        else:
            parts = []
            for host, e in sorted(spokes.items()):
                sm = e.get("summary") or {}
                parts.append(f"{host} {_age(e.get('age_s'))} {sm.get('status', '?')}"
                             f" {sm.get('pushed', 0)}↑ {sm.get('pulled', 0)}↓")
            spokes_s = " · ".join(parts)
        lines.append(f"{name:<{width}}  {lease_s:<28}  {spokes_s}")
    hosts = st.get("hosts") or {}
    if hosts:
        lines.append("")
        lines.append("spokes: " + ", ".join(
            f"{h} last seen {_age(now - v['last_seen'])} ({v['profiles']} profile(s))"
            for h, v in sorted(hosts.items())))
    return "\n".join(lines)


def cmd_hub(args: argparse.Namespace) -> int:
    config_path = getattr(args, "config", None)
    try:
        hub, profiles = _load_hub(config_path)
    except HomesyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.hub_cmd == "init":
        try:
            info = hub_init(hub, profiles)
        except HomesyncError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"hub {info['host']}: root {info['root']}, {len(info['profiles'])} file profile(s)"
              + (f", new trees: {', '.join(info['new_trees'])}" if info["new_trees"] else ""),
              file=sys.stderr)
        if getattr(args, "seed", False):
            # R42: the hub tree is seeded from THIS home by an ordinary spoke run
            # (additive: everything local flows into the empty tree, nothing comes back).
            print("seeding the hub tree from this home (fsync sync run --all) …", file=sys.stderr)
            return hs._cmd_run(argparse.Namespace(
                all=True, profile=None, dry_run=bool(getattr(args, "dry_run", False)),
                workers=None, notify=False, plan_only=False, config=config_path))
        if "spoke" in hub.roles:
            print("next: `fsync sync run --all` on this box seeds the tree from this home; "
                  "spokes then run the same command", file=sys.stderr)
        else:
            print("next: hub-only box — seed the tree with `fsync sync run --all` on the producer "
                  "spoke (R42); `fsync hub status` here shows each spoke's last run", file=sys.stderr)
        return 0

    if args.hub_cmd == "status":
        if not hub.local and not hs.peer_reachable(hub):
            print(f"{hub.label} not reachable on any address", file=sys.stderr)
            return 1
        try:
            st = hub_control(hub, config_path).status()
        except HomesyncError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001 — HubUnavailable / HubApiError from the client
            print(f"error: hub control plane: {e}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(json.dumps({"hub": hub.host, "local": hub.local, **st}, indent=2))
        else:
            print(render_status(hub, st, profiles))
        return 0

    print("usage: fsync hub {init,status}", file=sys.stderr)
    return 2
