"""P7.1 hub-sync (docs/hub-sync.md): config v2 → Hub, remote-root mapping,
sqlite leases, hub daemon routes, the stdlib hub client, and the acceptance
scenario — two homes converging through one hub on this box (local mode)."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from fsync import homesync as hs
from fsync.homesync import HomesyncError, Hub, Peer, Profile, load_config
from fsync.hubstate import (
    DEFAULT_LEASE_TTL, HubState, Lease, LeaseHeld, LeaseLost, LocalHubControl, MIN_LEASE_TTL,
)

HOST = socket.gethostname().split(".")[0]


def _cfg(tmp_path: Path, text: str, name: str = "sync-profiles.yaml") -> str:
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def _v2(tmp_path: Path, *, hub_host: str, roles: str, root: str | None = None,
        extra_hub: str = "", profiles: str = "    docs: {paths: [Documents]}\n") -> str:
    root_line = f"  root: {root}\n" if root else ""
    return _cfg(tmp_path, f"""
version: 2
requires: [hub-sync]
hub:
  host: {hub_host}
  ssh: developer@{hub_host}.lan
  addresses: [{hub_host}.lan, {hub_host}.tail.ts.net]
{root_line}{extra_hub}
hosts:
  {HOST}: {{ssh: developer@{HOST}.lan, role: {roles}}}
shared:
  defaults: {{conflict: newer}}
  profiles:
{profiles}""")


# --------------------------------------------------------------------------- #
# config v2                                                                   #
# --------------------------------------------------------------------------- #

def test_v2_hub_box_loads_local_hub(tmp_path):
    peer, profiles, _ = load_config(_v2(tmp_path, hub_host=HOST, roles="[hub, spoke]",
                                        root="/srv/hub",
                                        extra_hub="  retention: {tombstones: 90d}\n"))
    assert isinstance(peer, Hub)
    assert peer.local is True
    assert peer.roles == ["hub", "spoke"]
    assert peer.root == "/srv/hub"
    assert peer.user == "developer"
    assert peer.all_addresses() == [f"{HOST}.lan", f"{HOST}.tail.ts.net"]
    assert peer.retention == {"tombstones": "90d"}
    assert "docs" in profiles and profiles["docs"].direction == "both"
    assert "this box" in peer.label


def test_v2_spoke_loads_remote_hub(tmp_path):
    peer, _, _ = load_config(_v2(tmp_path, hub_host="otherbox", roles="[spoke]"))
    assert isinstance(peer, Hub) and peer.local is False
    assert peer.host == "otherbox" and peer.user == "developer"
    assert peer.target == "developer@otherbox"           # host until a probe picks an address
    assert peer.spec("/x") == "developer@otherbox:/x"
    assert peer.root == "~/.fsync/hub"                     # default


def test_v2_role_must_match_hub_host(tmp_path):
    with pytest.raises(HomesyncError, match="does not declare role"):
        load_config(_v2(tmp_path, hub_host=HOST, roles="[spoke]"))
    with pytest.raises(HomesyncError, match="declares the hub role"):
        load_config(_v2(tmp_path, hub_host="otherbox", roles="[hub, spoke]"))
    with pytest.raises(HomesyncError, match="subset"):
        load_config(_v2(tmp_path, hub_host="otherbox", roles="[mesh]"))


def test_v2_needs_hub_block(tmp_path):
    with pytest.raises(HomesyncError, match="hub"):
        load_config(_cfg(tmp_path, f"""
version: 2
hosts: {{{HOST}: {{}}}}
shared: {{profiles: {{x: {{paths: [a]}}}}}}
"""))


def test_v1_config_still_a_plain_peer(tmp_path):
    peer, _, _ = load_config(_cfg(tmp_path, "peer: {host: h}\nprofiles: {x: {paths: [a]}}"))
    assert type(peer) is Peer and not isinstance(peer, Hub)
    assert peer.local is False and peer.spec("/p") == "h:/p"


def test_hub_sync_is_a_declared_feature():
    from fsync import FEATURES
    assert "hub-sync" in FEATURES


# --------------------------------------------------------------------------- #
# remote-root mapping                                                         #
# --------------------------------------------------------------------------- #

def test_remote_roots_hub_vs_peer(monkeypatch):
    monkeypatch.setattr(hs, "peer_home", lambda p: "/home/dev")
    prof = Profile(name="docs", paths=["Documents"])
    peer = Peer(host="h")
    hub = Hub(host="h", root="~/.fsync/hub")
    assert hs.remote_root_for(peer, prof, "Documents") == "/home/dev/Documents"
    assert hs.remote_root_for(peer, prof, ".") == "/home/dev"
    assert hs.remote_root_for(hub, prof, "Documents") == "/home/dev/.fsync/hub/docs/tree/Documents"
    assert hs.remote_root_for(hub, prof, ".") == "/home/dev/.fsync/hub/docs/tree"
    assert hs.remote_backup_for(peer, prof, "R1") == "/home/dev/.fsync/backups/R1/docs"
    assert hs.remote_backup_for(hub, prof, "R1") == "/home/dev/.fsync/hub/backups/R1/docs"
    assert hs.hub_root(Hub(host="h", root="/srv/hub/")) == "/srv/hub"


def test_local_mode_plumbing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hub = Hub(host=HOST, local=True)
    assert hs.peer_reachable(hub) is True
    assert hs.peer_home(hub) == str(tmp_path)
    assert hs.peer_busy(hub) is False
    hs.ensure_peer_engine(hub)  # no-op, no ssh
    proc = hs._ssh(hub, "printf %s \"$HOME\"")
    assert proc.returncode == 0 and proc.stdout == str(tmp_path)
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "f.txt").write_text("x")
    recs = hs.remote_index(hub, str(tmp_path / "t"), recursive=True, exclude=[])
    assert [r["path"] for r in recs] == ["f.txt"]
    assert hs.remote_index(hub, str(tmp_path / "absent"), recursive=True, exclude=[]) == []


# --------------------------------------------------------------------------- #
# sqlite leases + run records                                                 #
# --------------------------------------------------------------------------- #

def test_lease_lifecycle(tmp_path):
    st = HubState(tmp_path / "hub" / "state.sqlite")
    a = st.acquire("docs", "fury4dx", "R1", ttl=600, now=1000.0)
    assert a["expires"] == 1600.0
    # another spoke: held
    with pytest.raises(LeaseHeld) as ei:
        st.acquire("docs", "omen4dx", "R2", ttl=600, now=1100.0)
    assert ei.value.holder == "fury4dx" and ei.value.run_id == "R1"
    # same spoke + run re-acquires (idempotent restart of the same run)
    again = st.acquire("docs", "fury4dx", "R1", ttl=600, now=1200.0)
    assert again["lease_id"] == a["lease_id"] and again["expires"] == 1800.0
    # other profiles are independent
    st.acquire("tools", "omen4dx", "R2", ttl=600, now=1200.0)
    # renew extends; renew with a stale id is lost
    r = st.renew("docs", a["lease_id"], ttl=600, now=1500.0)
    assert r["expires"] == 2100.0
    with pytest.raises(LeaseLost):
        st.renew("docs", "deadbeef", ttl=600, now=1500.0)
    # expiry: the TTL is the unlock (R36) — a crashed spoke never needs a manual reset
    b = st.acquire("docs", "omen4dx", "R3", ttl=600, now=2200.0)
    assert b["lease_id"] != a["lease_id"]
    with pytest.raises(LeaseLost):
        st.renew("docs", a["lease_id"], ttl=600, now=2300.0)
    assert st.release("docs", a["lease_id"]) is False   # not ours any more
    assert st.release("docs", b["lease_id"]) is True
    assert "docs" not in st.leases(now=2300.0)
    assert "tools" in st.leases(now=1700.0)


def test_lease_ttl_clamped(tmp_path):
    st = HubState(tmp_path / "s.sqlite")
    assert st.acquire("p", "h", "r", ttl=1, now=0.0)["ttl"] == MIN_LEASE_TTL
    assert st.acquire("q", "h", "r", ttl=None, now=0.0)["ttl"] == DEFAULT_LEASE_TTL


def test_run_records_and_status(tmp_path):
    st = HubState(tmp_path / "s.sqlite")
    st.record_run("fury4dx", "docs", "R1", {"status": "ok", "pushed": 3, "pulled": 0}, now=100.0)
    st.record_run("minis4dx", "docs", "R2", {"status": "ok", "pushed": 0, "pulled": 3}, now=160.0)
    st.record_run("fury4dx", "docs", "R3", {"status": "attention"}, now=200.0)  # upsert
    st.acquire("tools", "omen4dx", "R9", ttl=600, now=200.0)
    s = st.status(now=260.0)
    docs = s["profiles"]["docs"]
    assert docs["lease"] is None
    assert docs["spokes"]["fury4dx"]["run_id"] == "R3"
    assert docs["spokes"]["fury4dx"]["age_s"] == 60
    assert docs["spokes"]["minis4dx"]["summary"]["pulled"] == 3
    assert s["profiles"]["tools"]["lease"]["host"] == "omen4dx"
    assert s["hosts"]["fury4dx"]["last_seen"] == 200.0
    # the hub CLI renders it without blowing up
    from fsync.hub import render_status
    text = render_status(Hub(host="minis4dx", local=True), s,
                         {"docs": Profile(name="docs", paths=["D"]),
                          "tools": Profile(name="tools", paths=["t"])})
    assert "fury4dx 60s ago attention" in text and "omen4dx" in text


def test_lease_context_manager_acquires_and_releases():
    calls: list[tuple] = []

    class Fake:
        def acquire(self, profile, host, run_id, ttl):
            calls.append(("acquire", profile, host, run_id, ttl))
            return {"lease_id": "L1"}

        def renew(self, *a):
            calls.append(("renew",) + a)

        def release(self, profile, lease_id):
            calls.append(("release", profile, lease_id))
            return True

    with Lease(Fake(), "docs", "fury4dx", "R1", ttl=600) as lease:
        assert lease.lease_id == "L1"
    assert calls[0] == ("acquire", "docs", "fury4dx", "R1", 600)
    assert calls[-1] == ("release", "docs", "L1")
    assert lease.lost is None


# --------------------------------------------------------------------------- #
# hub daemon routes                                                           #
# --------------------------------------------------------------------------- #

def test_daemon_hub_routes(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from fsync.daemon import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _v2(tmp_path, hub_host=HOST, roles="[hub, spoke]", root=str(tmp_path / "hub"))
    app = create_app(cfg, require_token=False)
    c = TestClient(app)

    r = c.post("/v1/hub/docs/lease", json={"host": "fury4dx", "run_id": "R1", "ttl": 600})
    assert r.status_code == 200, r.text
    lease_id = r.json()["lease_id"]
    held = c.post("/v1/hub/docs/lease", json={"host": "omen4dx", "run_id": "R2"})
    assert held.status_code == 409
    assert held.json()["detail"]["holder"] == "fury4dx"
    renewed = c.post("/v1/hub/docs/lease", json={"lease_id": lease_id, "ttl": 900})
    assert renewed.status_code == 200 and renewed.json()["lease_id"] == lease_id
    assert c.post("/v1/hub/docs/lease", json={"lease_id": "nope"}).status_code == 410
    assert c.post("/v1/hub/docs/lease", json={}).status_code == 400
    assert c.post("/v1/hub/docs/runs", json={"host": "fury4dx", "run_id": "R1",
                                             "summary": {"status": "ok", "pushed": 2}}).json() == {"recorded": True}
    st = c.get("/v1/hub/status").json()
    assert st["host"] == HOST and st["root"] == str(tmp_path / "hub")
    assert st["profiles"]["docs"]["lease"]["host"] == "fury4dx"
    assert st["profiles"]["docs"]["spokes"]["fury4dx"]["summary"]["pushed"] == 2
    assert c.delete(f"/v1/hub/docs/lease/{lease_id}").json() == {"released": True}
    assert c.get("/v1/hub/status").json()["profiles"]["docs"]["lease"] is None
    # the state file landed under the hub root
    assert (tmp_path / "hub" / "state.sqlite").exists()


def test_daemon_without_hub_role_has_no_hub_routes(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from fsync.daemon import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _v2(tmp_path, hub_host="otherbox", roles="[spoke]")   # a spoke
    c = TestClient(create_app(cfg, require_token=False))
    assert c.get("/v1/hub/status").status_code == 404
    v1 = _cfg(tmp_path, "peer: {host: h}\nprofiles: {x: {paths: [a]}}", "v1.yaml")
    assert TestClient(create_app(v1, require_token=False)).get("/v1/hub/status").status_code == 404


# --------------------------------------------------------------------------- #
# stdlib hub client (plain http against a canned server; TLS is certs.py's)   #
# --------------------------------------------------------------------------- #

class _Canned(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, dict | None]] = []

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict | None:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_GET(self):
        _Canned.seen.append(("GET", self.path, None))
        self._send(200, {"profiles": {}, "hosts": {}, "now": 1.0})

    def do_POST(self):
        body = self._body()
        _Canned.seen.append(("POST", self.path, body))
        if self.path.endswith("/lease"):
            if body.get("lease_id") == "stale":
                return self._send(410, {"detail": "lease stale is no longer current"})
            if body.get("host") == "loser":
                return self._send(409, {"detail": {"holder": "fury4dx", "run_id": "R1",
                                                    "expires": 99.0, "message": "held"}})
            return self._send(200, {"lease_id": "L1", "expires": 100.0})
        self._send(200, {"recorded": True})

    def do_DELETE(self):
        _Canned.seen.append(("DELETE", self.path, None))
        self._send(200, {"released": True})

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture
def canned_server():
    srv = HTTPServer(("127.0.0.1", 0), _Canned)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _Canned.seen.clear()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_hub_client_roundtrip_and_failover(canned_server):
    from fsync.hubclient import HubClient, RemoteHubControl
    # first address is a dead port: the client must move on and remember the live one
    dead = "http://127.0.0.1:9"
    client = HubClient([dead, canned_server])
    client.timeout = 5
    ctl = RemoteHubControl(client)
    granted = ctl.acquire("docs", "omen4dx", "R2", 600)
    assert granted["lease_id"] == "L1"
    assert client.active == canned_server
    assert _Canned.seen[-1] == ("POST", "/v1/hub/docs/lease",
                                {"host": "omen4dx", "run_id": "R2", "ttl": 600})
    with pytest.raises(LeaseHeld) as ei:
        ctl.acquire("docs", "loser", "R3", 600)
    assert ei.value.holder == "fury4dx" and ei.value.expires == 99.0
    with pytest.raises(LeaseLost):
        ctl.renew("docs", "stale", 600)
    assert ctl.renew("docs", "L1", 600)["lease_id"] == "L1"
    assert ctl.release("docs", "L1") is True
    assert _Canned.seen[-1][1] == "/v1/hub/docs/lease/L1"
    ctl.record_run("fury4dx", "docs", "R1", {"status": "ok"})
    assert _Canned.seen[-1][2] == {"host": "fury4dx", "run_id": "R1", "summary": {"status": "ok"}}
    assert ctl.status()["profiles"] == {}


def test_hub_client_unreachable():
    from fsync.hubclient import HubClient, HubUnavailable
    with pytest.raises(HubUnavailable):
        HubClient(["http://127.0.0.1:9"]).status()


# --------------------------------------------------------------------------- #
# acceptance: two homes converge through one hub on this box                 #
# --------------------------------------------------------------------------- #

def _run_args(cfg: str, **kw) -> argparse.Namespace:
    base = dict(all=True, profile=None, dry_run=False, workers=1, notify=False,
                plan_only=False, config=cfg)
    base.update(kw)
    return argparse.Namespace(**base)


def _be(monkeypatch, home: Path) -> None:
    """Become the box whose $HOME is ``home``: its own state root too (each real
    box keeps its own prev-index / runs / locks — sharing one would make B's
    first pull look like a resurrection of what A indexed)."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(hs, "STATE_ROOT", str(home / ".local" / "state" / "fsync"))


def _latest_report() -> dict:
    rid = (hs.state_root() / "runs" / "latest").read_text().strip()
    return json.loads((hs.state_root() / "runs" / rid / "report.json").read_text())


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_two_homes_converge_through_local_hub(tmp_path, monkeypatch):
    hub_root = tmp_path / "hub"
    cfg = _v2(tmp_path, hub_host=HOST, roles="[hub, spoke]", root=str(hub_root))
    home_a, home_b = tmp_path / "homeA", tmp_path / "homeB"
    for h in (home_a, home_b):
        (h / "Documents").mkdir(parents=True)
    (home_a / "Documents" / "a.txt").write_text("from A")

    # `fsync hub init` lays the tree + state down (idempotent) ...
    from fsync.hub import hub_init
    _be(monkeypatch, home_a)
    hub, profiles, _ = load_config(cfg)
    info = hub_init(hub, profiles)
    assert info["new_trees"] == ["docs"]
    assert (hub_root / "docs" / "tree").is_dir() and (hub_root / "state.sqlite").exists()
    assert hub_init(hub, profiles)["new_trees"] == []

    # ... box A runs: its file lands in the hub tree (R42 seed = an ordinary run)
    assert hs._cmd_run(_run_args(cfg)) == 0
    assert (hub_root / "docs" / "tree" / "Documents" / "a.txt").read_text() == "from A"
    rep = _latest_report()
    assert rep["peer"] == {"host": HOST, "user": "developer", "hub": True, "local": True}
    assert rep["profiles"]["docs"]["status"] == "ok"
    assert rep["profiles"]["docs"]["paths"]["Documents"]["a_to_b"]["files_transferred"] == 1

    # box B runs against the same hub: pulls A's file, then adds its own
    _be(monkeypatch, home_b)
    assert hs._cmd_run(_run_args(cfg)) == 0
    assert (home_b / "Documents" / "a.txt").read_text() == "from A"
    (home_b / "Documents" / "b.txt").write_text("from B")
    assert hs._cmd_run(_run_args(cfg)) == 0
    assert (hub_root / "docs" / "tree" / "Documents" / "b.txt").read_text() == "from B"

    # back on A: B's file arrives; A never talked to B (R30)
    _be(monkeypatch, home_a)
    assert hs._cmd_run(_run_args(cfg)) == 0
    assert (home_a / "Documents" / "b.txt").read_text() == "from B"
    rep = _latest_report()
    assert rep["profiles"]["docs"]["status"] == "ok"
    assert rep["profiles"]["docs"]["paths"]["Documents"]["resurrected"]["count"] == 0

    # the hub remembers each spoke's last run (R41) — same host here, so one row
    st = HubState(hub_root / "state.sqlite").status()
    spoke = st["profiles"]["docs"]["spokes"][HOST]
    assert spoke["summary"]["status"] == "ok" and spoke["summary"]["pulled"] == 1
    assert st["leases"] == {}   # released after the run


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_profile_held_elsewhere_is_deferred_not_failed(tmp_path, monkeypatch):
    hub_root = tmp_path / "hub"
    cfg = _v2(tmp_path, hub_host=HOST, roles="[hub, spoke]", root=str(hub_root),
              profiles="    docs: {paths: [Documents]}\n    tools: {paths: [tools]}\n")
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "tools").mkdir()
    (home / "Documents" / "a.txt").write_text("A")
    (home / "tools" / "t.sh").write_text("T")
    monkeypatch.setenv("HOME", str(home))

    other = HubState(hub_root / "state.sqlite")
    other.acquire("docs", "omen4dx", "R-other", ttl=600)

    assert hs._cmd_run(_run_args(cfg)) == 0           # deferral is not a failure
    rep = _latest_report()
    assert rep["profiles"]["docs"]["status"] == "deferred"
    assert "omen4dx" in rep["profiles"]["docs"]["reason"]
    assert not (hub_root / "docs" / "tree" / "Documents" / "a.txt").exists()
    assert (hub_root / "tools" / "tree" / "tools" / "t.sh").exists()   # others still ran
    assert rep["errors"] == []


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_overwrite_through_hub_backs_up_loser(tmp_path, monkeypatch):
    """conflict: newer through the hub (R35): the newer spoke copy wins and the
    hub keeps the overwritten tree file under its own backups (R4)."""
    hub_root = tmp_path / "hub"
    cfg = _v2(tmp_path, hub_host=HOST, roles="[hub, spoke]", root=str(hub_root))
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    f = home / "Documents" / "n.txt"
    f.write_text("v1")
    monkeypatch.setenv("HOME", str(home))
    assert hs._cmd_run(_run_args(cfg)) == 0
    time.sleep(1.1)                          # mtime resolution: make v2 strictly newer
    f.write_text("v2-newer")
    assert hs._cmd_run(_run_args(cfg)) == 0
    assert (hub_root / "docs" / "tree" / "Documents" / "n.txt").read_text() == "v2-newer"
    backups = list((hub_root / "backups").rglob("n.txt"))
    assert len(backups) == 1 and backups[0].read_text() == "v1"


def test_hub_only_box_never_syncs_its_own_home(tmp_path, monkeypatch, capsys):
    """A file server with role [hub] alone serves the spokes; `sync run` there is
    a clean no-op (no run dir, nothing pulled into its empty home), hub init works."""
    hub_root = tmp_path / "hub"
    cfg = _v2(tmp_path, hub_host=HOST, roles="[hub]", root=str(hub_root))
    home = tmp_path / "home"
    home.mkdir()
    _be(monkeypatch, home)
    hub, profiles, _ = load_config(cfg)
    assert hub.local and hub.roles == ["hub"]
    # seed the tree as a spoke would have
    (hub_root / "docs" / "tree" / "Documents").mkdir(parents=True)
    (hub_root / "docs" / "tree" / "Documents" / "a.txt").write_text("A")
    assert hs._cmd_run(_run_args(cfg)) == 0
    assert "hub-only" in capsys.readouterr().err
    assert not (home / "Documents").exists()
    assert not (hs.state_root() / "runs").exists()
    assert hs._cmd_run(_run_args(cfg, plan_only=True)) == 0
    assert json.loads(capsys.readouterr().out)["hub_only"] is True
    from fsync.hub import hub_init
    assert hub_init(hub, profiles)["profiles"] == ["docs"]


def test_hub_cli_status_json_local(tmp_path, monkeypatch, capsys):
    from fsync.hub import cmd_hub
    hub_root = tmp_path / "hub"
    cfg = _v2(tmp_path, hub_host=HOST, roles="[hub, spoke]", root=str(hub_root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cmd_hub(argparse.Namespace(hub_cmd="init", seed=False, dry_run=False, config=cfg)) == 0
    assert json.loads((hub_root / "hub.json").read_text())["profiles"] == ["docs"]
    assert cmd_hub(argparse.Namespace(hub_cmd="status", json=True, config=cfg)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hub"] == HOST and out["local"] is True and out["leases"] == {}


def test_hub_cli_refuses_v1_config(tmp_path, capsys):
    from fsync.hub import cmd_hub
    v1 = _cfg(tmp_path, "peer: {host: h}\nprofiles: {x: {paths: [a]}}")
    assert cmd_hub(argparse.Namespace(hub_cmd="status", json=False, config=v1)) == 2
    assert "no hub" in capsys.readouterr().err


def test_pyz_carries_the_hub_stack(tmp_path):
    """Cutover finding 2026-11-08: certs.py was missing from CORE_MODULES, so the
    deployed spoke could not even import fsync.hub. Build the pyz and import the
    whole spoke-side hub stack from a neutral cwd (the repo dir would shadow it)."""
    import subprocess, sys
    from fsync import meta_deploy as md
    assert "certs.py" in md.CORE_MODULES
    out = md.build_pyz(tmp_path / "fsync.pyz")["path"]
    proc = subprocess.run(
        [sys.executable, "-c",
         "import fsync.hub, fsync.hubclient, fsync.hubstate, fsync.certs, fsync.cli; print('ok')"],
        cwd=str(tmp_path), env={"PYTHONPATH": str(out), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr
    # `fsync daemon cert` from the pyz needs no fastapi
    proc = subprocess.run([sys.executable, str(out), "daemon", "cert"], cwd=str(tmp_path),
                          env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "SHA256" in proc.stdout, proc.stderr


def test_peer_baseline_is_scoped_to_the_hub(tmp_path, monkeypatch):
    """Cutover finding: a pairwise baseline must not judge the hub tree. The
    peer-side prev-index for a Hub lives under its own name; a v1 peer's does
    not move (no spurious reset for a fleet that stays pairwise)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    hs._save_prev_index("docs", "D", "peer", {"old.txt"}, "R0")                       # v1 baseline
    assert hs._load_prev_index("docs", "D", "peer") == {"old.txt"}
    assert hs._load_prev_index("docs", "D", "peer", "hub-files4dx") is None           # fresh for the hub
    plan = {"index_a": {"old.txt"}, "index_b": set()}
    res = hs.detect_resurrections("docs", "D", plan, ["old.txt"], [], set(), counterpart="hub-files4dx")
    assert res["count"] == 0 and res["baseline"]["peer"] is False
    res_v1 = hs.detect_resurrections("docs", "D", plan, ["old.txt"], [], set())
    assert res_v1["on_peer"] == ["old.txt"]
    assert hs._prev_index_path("docs", "D", "peer", "hub-files4dx").name == "D.peer@hub-files4dx.json.gz"
