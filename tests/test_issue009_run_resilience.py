"""ISSUE-009: a hung peer must not take the whole scheduled run down.

The incident (minis, 2026-08-30 23:08): fury went to sleep while minis was
indexing it over ssh; after 900 s ``subprocess.TimeoutExpired`` escaped
``_cmd_run`` — no report.json, no notification, ``runs/latest`` never moved.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from fsync import homesync as hs


def _hang(*args, **kwargs):
    raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 5))


def test_ssh_timeout_is_a_homesync_error(monkeypatch):
    monkeypatch.setattr(hs.subprocess, "run", _hang)
    with pytest.raises(hs.HomesyncError, match="no reply within 5s"):
        hs._ssh_to("dev@peer", "python3 -m fsync.cli index /x", timeout=5)


def test_peer_probes_treat_timeout_as_away_or_busy(monkeypatch):
    monkeypatch.setattr(hs.subprocess, "run", _hang)
    peer = hs.Peer(host="peer.lan", user="dev", home="/remote")
    assert hs.peer_reachable(peer) is False
    assert hs.peer_busy(peer) is True


def test_run_confines_an_unexpected_profile_error(tmp_path, monkeypatch):
    peer = hs.Peer(host="peer.lan", user="dev", home="/remote")
    profiles = {"a": hs.Profile(name="a", paths=["w"]), "b": hs.Profile(name="b", paths=["w"])}
    monkeypatch.setattr(hs, "load_config", lambda p: (peer, profiles, {}))
    monkeypatch.setattr(hs, "peer_reachable", lambda p: True)
    monkeypatch.setattr(hs, "peer_busy", lambda p: False)
    monkeypatch.setattr(hs, "ensure_peer_engine", lambda p: None)
    notes = []
    monkeypatch.setattr(hs, "_notify", lambda s, b, **kw: notes.append((s, b)))

    def fake_run_profile(peer, prof, **kw):
        if prof.name == "a":
            raise subprocess.TimeoutExpired(cmd="rsync", timeout=1)   # raw, not HomesyncError
        return {"conflict": prof.conflict, "paths": {}, "status": "ok"}
    monkeypatch.setattr(hs, "run_profile", fake_run_profile)

    args = SimpleNamespace(config=None, all=True, profile=None, dry_run=False, workers=None,
                           notify=True, plan_only=False, plan_progress=None, verbose=False)
    rc = hs._cmd_run(args)
    assert rc == 1
    run_id = (hs.state_root() / "runs" / "latest").read_text().strip()
    report = json.loads((hs.state_root() / "runs" / run_id / "report.json").read_text())
    assert report["profiles"]["a"]["status"] == "error"
    assert report["profiles"]["b"]["status"] == "ok"               # the run went on
    assert report["errors"] == ["a: unexpected TimeoutExpired: Command 'rsync' timed out after 1 seconds"]
    assert notes and notes[0][0].startswith("fsync sync FAILED on ")
