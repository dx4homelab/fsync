"""Tests for the P1 home-sync engine pieces: excludes, hash cache,
index-file compare, rename suppression, and profile loading.
(docs/home-sync-automation.md)"""

import json
import sys
from pathlib import Path
from subprocess import run, PIPE

import pytest

from fsync.fileindex import (
    build_sync_plan,
    default_cache_path,
    list_files_with_metadata,
    load_index_file,
    matches_exclude,
)
from fsync.homesync import HomesyncError, load_config


CLI = [sys.executable, "-m", "fsync.cli"]


# --------------------------------------------------------------------------- #
# excludes                                                                    #
# --------------------------------------------------------------------------- #

def test_matches_exclude_semantics():
    # component pattern: matches anywhere in the tree
    assert matches_exclude("a/b/x.tmp", ["*.tmp"])
    assert matches_exclude(".git/config", [".git"])
    assert matches_exclude("sub/.git/config", [".git"])
    assert not matches_exclude("gitfile", [".git"])
    # path pattern: anchored to the relative path
    assert matches_exclude("backups/x/y", ["backups/*"])
    assert not matches_exclude("deep/backups/x", ["backups/*"])


def test_index_excludes_files(tmp_path):
    (tmp_path / "keep.txt").write_text("keep")
    (tmp_path / "drop.tmp").write_text("drop")
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "old.txt").write_text("old")
    recs = list_files_with_metadata(tmp_path, exclude=["*.tmp", "backups/*"])
    assert [r["path"] for r in recs] == ["keep.txt"]


# --------------------------------------------------------------------------- #
# hash cache                                                                  #
# --------------------------------------------------------------------------- #

def test_cache_reuses_and_invalidates(tmp_path):
    root = tmp_path / "root"  # cache lives OUTSIDE the indexed root
    root.mkdir()
    f = root / "data.bin"
    f.write_bytes(b"v1-content")
    cache = tmp_path / "cache.json"

    first = list_files_with_metadata(root, cache_path=cache)
    assert first[0]["hash"] is not None
    saved = json.loads(cache.read_text())
    assert saved["files"]["data.bin"][2] == first[0]["hash"]

    # Poison the cached digest: an unchanged file must reuse it (proves no re-read),
    # a touched file must be re-hashed (proves size/mtime invalidation).
    saved["files"]["data.bin"][2] = "poisoned"
    cache.write_text(json.dumps(saved))
    assert list_files_with_metadata(root, cache_path=cache)[0]["hash"] == "poisoned"

    f.write_bytes(b"v2-content-longer")
    rehashed = list_files_with_metadata(root, cache_path=cache)[0]["hash"]
    assert rehashed != "poisoned"
    assert json.loads(cache.read_text())["files"]["data.bin"][2] == rehashed


def test_cache_ignored_on_algo_change(tmp_path):
    (tmp_path / "f").write_bytes(b"x")
    cache = tmp_path / "c.json"
    list_files_with_metadata(tmp_path, cache_path=cache, hash_algo="sha256")
    sha_hash = json.loads(cache.read_text())["files"]["f"][2]
    md5 = list_files_with_metadata(tmp_path, cache_path=cache, hash_algo="md5")[0]["hash"]
    assert md5 != sha_hash and len(md5) == 32


def test_default_cache_path_is_stable_per_root(tmp_path):
    assert default_cache_path(tmp_path) == default_cache_path(tmp_path)
    assert default_cache_path(tmp_path) != default_cache_path(tmp_path / "sub")


# --------------------------------------------------------------------------- #
# index-file loading + compare from files                                     #
# --------------------------------------------------------------------------- #

def test_load_index_file_json_and_jsonl(tmp_path):
    recs = [{"path": "a", "hash": "h1"}, {"path": "b", "hash": "h2"}]
    j = tmp_path / "i.json"
    j.write_text(json.dumps(recs))
    jl = tmp_path / "i.jsonl"
    jl.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    assert load_index_file(j) == recs
    assert load_index_file(jl) == recs


def test_compare_accepts_index_files(tmp_path):
    a_dir = tmp_path / "a"
    a_dir.mkdir()
    (a_dir / "same.txt").write_text("same")
    (a_dir / "only_a.txt").write_text("A")
    b_dir = tmp_path / "b"
    b_dir.mkdir()
    (b_dir / "same.txt").write_text("same")
    (b_dir / "only_b.txt").write_text("B")

    idx_a = tmp_path / "a.json"
    idx_b = tmp_path / "b.json"
    for d, out in ((a_dir, idx_a), (b_dir, idx_b)):
        r = run([*CLI, "index", str(d), "--output", str(out)], stdout=PIPE, stderr=PIPE, text=True)
        assert r.returncode == 0, r.stderr

    r = run([*CLI, "compare", str(idx_a), str(idx_b)], stdout=PIPE, stderr=PIPE, text=True)
    assert r.returncode == 0, r.stderr
    rep = json.loads(r.stdout)
    assert [i["path"] for i in rep["only_in_a"]] == ["only_a.txt"]
    assert [i["path"] for i in rep["only_in_b"]] == ["only_b.txt"]
    assert len(rep["exact_matches"]) == 1


def test_compare_index_files_respects_exclude(tmp_path):
    idx = [{"path": "keep.txt", "name": "keep.txt", "hash": "h"},
           {"path": "junk.tmp", "name": "junk.tmp", "hash": "j"}]
    fa = tmp_path / "a.json"
    fa.write_text(json.dumps(idx))
    fb = tmp_path / "b.json"
    fb.write_text(json.dumps([]))
    r = run([*CLI, "compare", str(fa), str(fb), "--exclude", "*.tmp"],
            stdout=PIPE, stderr=PIPE, text=True)
    assert r.returncode == 0, r.stderr
    assert [i["path"] for i in json.loads(r.stdout)["only_in_a"]] == ["keep.txt"]


# --------------------------------------------------------------------------- #
# rename suppression                                                          #
# --------------------------------------------------------------------------- #

def _item(path, mtime=1.0, h="h", size=1000):
    return {"path": path, "name": path.split("/")[-1], "mtime": mtime, "hash": h, "size": size}


def test_unique_large_rename_survives():
    rep = {"exact_matches": [], "name_matches_diff_hash": [], "only_in_a": [], "only_in_b": [],
           "hash_matches_diff_name": [(_item("a/x.bin", h="dup"), _item("b/y.bin", h="dup"))]}
    plan = build_sync_plan(rep, rename_min_size=64)
    assert len(plan["renames"]) == 1
    assert plan["renames_suppressed"] == []


def test_duplicate_content_rename_suppressed_to_copies():
    # The .claude hazard: the pair's hash also exists elsewhere (lock files,
    # boilerplate), so the "rename" would pair unrelated files.
    dup_elsewhere = _item("tasks/other/.lock", h="empty")
    rep = {"exact_matches": [], "name_matches_diff_hash": [],
           "only_in_a": [dup_elsewhere], "only_in_b": [],
           "hash_matches_diff_name": [(_item("a/.lock", h="empty"), _item("b/blob@v1", h="empty"))]}
    plan = build_sync_plan(rep, rename_min_size=0)
    assert plan["renames"] == []
    assert len(plan["renames_suppressed"]) == 1
    # demoted pair travels as ordinary union copies
    assert "a/.lock" in [i["path"] for i in plan["a_to_b"]]
    assert "b/blob@v1" in [i["path"] for i in plan["b_to_a"]]


def test_small_rename_suppressed_by_size_floor():
    rep = {"exact_matches": [], "name_matches_diff_hash": [], "only_in_a": [], "only_in_b": [],
           "hash_matches_diff_name": [(_item("a/f", h="u", size=3), _item("b/g", h="u", size=3))]}
    assert build_sync_plan(rep, rename_min_size=64)["renames"] == []
    assert len(build_sync_plan(rep, rename_min_size=64)["renames_suppressed"]) == 1
    # floor off -> unique pair survives
    assert len(build_sync_plan(rep, rename_min_size=0)["renames"]) == 1


def test_suppressed_rename_under_mirror_copies_and_deletes():
    rep = {"exact_matches": [], "name_matches_diff_hash": [], "only_in_a": [], "only_in_b": [],
           "hash_matches_diff_name": [(_item("a/f", h="u", size=3), _item("b/g", h="u", size=3))]}
    plan = build_sync_plan(rep, mirror="a-to-b", rename_min_size=64)
    assert [i["path"] for i in plan["a_to_b"]] == ["a/f"]
    assert [i["path"] for i in plan["b_delete"]] == ["b/g"]


# --------------------------------------------------------------------------- #
# profiles config                                                             #
# --------------------------------------------------------------------------- #

def _write_cfg(tmp_path, text):
    p = tmp_path / "profiles.yaml"
    p.write_text(text)
    return str(p)


def test_load_config_defaults_and_overrides(tmp_path):
    cfg = _write_cfg(tmp_path, """
peer: {host: peerbox, user: dev, home: /home/dev}
defaults: {conflict: newer, workers: 4, rename_min_size: 32}
profiles:
  docs: {paths: [Documents, Pictures]}
  claude:
    paths: [.claude]
    conflict: review
    exclude: [".credentials.json"]
""")
    peer, profiles, _ = load_config(cfg)
    assert peer.target == "dev@peerbox"
    assert profiles["docs"].conflict == "newer"
    assert profiles["docs"].workers == 4
    assert profiles["docs"].rename_min_size == 32
    assert profiles["claude"].conflict == "review"
    assert ".credentials.json" in profiles["claude"].exclude


def test_load_config_rejects_bad_input(tmp_path):
    with pytest.raises(HomesyncError, match="peer.host"):
        load_config(_write_cfg(tmp_path, "profiles: {x: {paths: [a]}}"))
    with pytest.raises(HomesyncError, match="paths"):
        load_config(_write_cfg(tmp_path, "peer: {host: h}\nprofiles: {x: {}}"))
    with pytest.raises(HomesyncError, match="relative"):
        load_config(_write_cfg(tmp_path, "peer: {host: h}\nprofiles: {x: {paths: [/abs]}}"))
    with pytest.raises(HomesyncError, match="conflict"):
        load_config(_write_cfg(tmp_path, "peer: {host: h}\nprofiles: {x: {paths: [a], conflict: chaos}}"))
    with pytest.raises(HomesyncError, match="no profiles config"):
        load_config(str(tmp_path / "missing.yaml"))


def test_sync_init_writes_starter_config(tmp_path):
    cfg = tmp_path / "sync-profiles.yaml"
    r = run([*CLI, "sync", "init", "--config", str(cfg)], stdout=PIPE, stderr=PIPE, text=True)
    assert r.returncode == 0, r.stderr
    peer, profiles, _ = load_config(str(cfg))
    assert peer.host == "minis4dx.lan"
    assert profiles["claude"].conflict == "review"
    # session ephemera must stay out of the claude profile (GC ping-pong)
    assert "file-history/*" in profiles["claude"].exclude
    # refuses to clobber without --force
    r2 = run([*CLI, "sync", "init", "--config", str(cfg)], stdout=PIPE, stderr=PIPE, text=True)
    assert r2.returncode == 2


# --------------------------------------------------------------------------- #
# P2: progress journal + run discovery                                        #
# --------------------------------------------------------------------------- #

import os

from fsync.homesync import ProgressWriter, discover_run, parse_progress_chunk
import fsync.homesync as hs


def test_parse_progress_chunk_variants():
    line = b"  1,234,567  45%   10.25MB/s    0:00:12 (xfr#5, ir-chk=10/200)\r"
    assert parse_progress_chunk(line) == (1234567, 45, 5, 200)
    line2 = b"        512 100%  500.00kB/s    0:00:00 (xfr#3, to-chk=0/3)\n"
    assert parse_progress_chunk(line2) == (512, 100, 3, 3)
    # bare progress line without xfr counter
    assert parse_progress_chunk(b"  99  3%   1.00MB/s    0:00:01") == (99, 3, None, None)
    assert parse_progress_chunk(b"building file list") is None
    # multiple lines in one chunk -> newest wins
    both = line + b"  2,000,000  80%   9.99MB/s    0:00:02 (xfr#9, ir-chk=1/200)\r"
    assert parse_progress_chunk(both) == (2000000, 80, 9, 200)


def test_progress_writer_and_discover(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "STATE_ROOT", str(tmp_path / "state"))
    run_dir = tmp_path / "state" / "runs" / "r1"
    run_dir.mkdir(parents=True)

    pw = ProgressWriter(run_dir, "r1", dry_run=False, profile_names=["docs"])
    pw.path_phase("docs", "Documents", "a_to_b", planned=3)
    pw.leg_progress("docs", "Documents", "a_to_b", 1024, 50, 1, 3)
    pw.write(force=True)

    found = discover_run()
    assert found is not None
    assert found["running"] is True  # our own live pid
    assert found["pointer"]["run_id"] == "r1"
    ps = found["progress"]["profiles"]["docs"]["paths"]["Documents"]
    assert ps["phase"] == "a_to_b" and ps["leg"]["bytes"] == 1024

    pw.path_done("docs", "Documents", {"conflicts": 2, "seconds": 1.5})
    pw.finish("done")
    found = discover_run()
    assert found["running"] is False  # status no longer 'running'
    assert found["progress"]["status"] == "done"
    assert found["progress"]["profiles"]["docs"]["paths"]["Documents"]["phase"] == "done"


def test_discover_run_dead_pid_is_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "STATE_ROOT", str(tmp_path / "state"))
    run_dir = tmp_path / "state" / "runs" / "r2"
    run_dir.mkdir(parents=True)
    pw = ProgressWriter(run_dir, "r2", dry_run=False, profile_names=["x"])
    # simulate a crashed runner: progress still 'running' but pid is gone
    ptr = json.loads(hs.current_run_pointer().read_text())
    ptr["pid"] = 2**22 + os.getpid()  # almost certainly not a live pid
    hs.current_run_pointer().write_text(json.dumps(ptr))
    found = discover_run()
    assert found is not None
    assert found["running"] is False
    assert found["progress"]["status"] == "running"  # stale -> UI shows aborted


# --------------------------------------------------------------------------- #
# P3: timer units                                                             #
# --------------------------------------------------------------------------- #

from fsync.homesync import render_timer_units


def test_render_timer_units():
    service, timer = render_timer_units("/opt/venv/bin/python", "30min")
    assert "ExecStart=/opt/venv/bin/python -m fsync.cli sync run --all --notify" in service
    assert "Type=oneshot" in service
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus" in service
    # OnUnitInactiveSec, not OnUnitActiveSec: oneshot units may never latch
    # "active", which silently stops monotonic rescheduling.
    assert "OnUnitInactiveSec=30min" in timer
    assert not any(line.startswith("OnUnitActiveSec") for line in timer.splitlines())
    assert "OnBootSec=" in timer and "RandomizedDelaySec=" in timer
    assert "WantedBy=timers.target" in timer


def test_tui_run_specs_direction_states():
    from fsync.tui import SyncTuiApp
    app = SyncTuiApp()
    app.plan = {"profiles": {"documents": {}, "tools": {}, "claude": {}}}
    app.state = {"documents": "both", "tools": "push", "claude": "off"}
    assert app.run_specs() == ["documents=both", "tools=push"]
    # digit cycle order: both -> push -> pull -> off -> both
    assert app.CYCLE["both"] == "push"
    assert app.CYCLE["push"] == "pull"
    assert app.CYCLE["pull"] == "off"
    assert app.CYCLE["off"] == "both"


def test_ui_imports_no_engine_modules():
    # R9 boundary: the TUI and API client must never pull in engine code.
    r = run([sys.executable, "-c",
             "import sys; import fsync.tui, fsync.client; "
             "bad = [m for m in sys.modules if m in "
             "('fsync.homesync', 'fsync.fileindex', 'fsync.daemon')]; "
             "sys.exit('engine leaked into UI: %s' % bad if bad else 0)"],
            stdout=PIPE, stderr=PIPE, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


def test_profile_direction_config_and_override(tmp_path):
    with pytest.raises(HomesyncError, match="direction"):
        load_config(_write_cfg(tmp_path, """
peer: {host: h}
profiles:
  c: {paths: [z], direction: sideways}
"""))
    _, profiles, _ = load_config(_write_cfg(tmp_path, """
peer: {host: h}
profiles:
  a: {paths: [x]}
  b: {paths: [y], direction: push}
"""))
    assert profiles["a"].direction == "both"
    assert profiles["b"].direction == "push"


def test_report_totals():
    from fsync.homesync import report_totals
    rep = {"profiles": {"p": {"paths": {
        "x": {"conflicts": 2, "a_to_b": {"files_transferred": 3}, "b_to_a": {"files_transferred": 1}},
        "y": {"conflicts": 0, "a_to_b": {}, "b_to_a": {"files_transferred": 4}},
    }}}, "errors": ["boom"]}
    assert report_totals(rep) == (8, 2, 1)
    assert report_totals({}) == (0, 0, 0)


def test_daemon_span_and_unit():
    from fsync.daemon import parse_span, render_daemon_unit
    assert parse_span("1h") == 3600
    assert parse_span("30min") == 1800
    assert parse_span("90s") == 90
    assert parse_span("1h30min") == 5400
    assert parse_span("garbage", 42) == 42
    unit = render_daemon_unit("/opt/venv/bin/python")
    assert "ExecStart=/opt/venv/bin/python -m fsync.cli daemon run" in unit
    assert "Restart=always" in unit and "WantedBy=default.target" in unit


def test_certs_generation_and_trust(tmp_path, monkeypatch):
    from fsync import certs
    monkeypatch.setattr(certs, "TLS_DIR", str(tmp_path / "tls"))
    cert, key = certs.ensure_cert()
    assert cert.exists() and key.exists()
    assert (key.stat().st_mode & 0o777) == 0o600
    fp = certs.cert_fingerprint(cert)
    assert len(fp.replace(":", "")) == 64  # sha256
    # idempotent: second call must not regenerate
    assert certs.ensure_cert()[0].read_text() == cert.read_text()
    # pin a "peer" (use our own cert as stand-in) and build the bundle
    with pytest.raises(certs.CertError):
        certs.trust_peer("bogus", "not a pem")
    certs.trust_peer("peerbox", cert.read_text())
    assert certs.trusted_peers() == ["peerbox"]
    assert certs.bundle_path().exists()


def test_daemon_api_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import fsync.homesync as hs
    import fsync.daemon as dm

    monkeypatch.setattr(hs, "STATE_ROOT", str(tmp_path / "state"))
    cfg = _write_cfg(tmp_path, """
peer: {host: peerbox, user: dev, home: /home/dev}
daemon: {port: 7555, interval: 30min}
profiles:
  docs: {paths: [Documents]}
""")
    # fabricate one finished run
    run_dir = tmp_path / "state" / "runs" / "20260704-010101-aaaa"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(json.dumps({
        "profiles": {"docs": {"paths": {"Documents": {
            "conflicts": 1, "a_to_b": {"files_transferred": 2}, "b_to_a": {}}}}},
        "errors": [], "finished": "x", "dry_run": False}))
    (run_dir / "progress.json").write_text(json.dumps(
        {"status": "done", "finished_ts": 1234.0, "run_id": run_dir.name}))
    (tmp_path / "state" / "runs" / "latest").write_text(run_dir.name)

    client = TestClient(dm.create_app(cfg))
    st = client.get("/v1/status").json()
    assert st["runner"] is None
    assert st["last"]["moved"] == 2 and st["last"]["conflicts"] == 1
    assert st["schedule"]["interval_s"] == 1800

    profs = client.get("/v1/profiles").json()
    assert profs["docs"]["direction"] == "both"

    runs = client.get("/v1/runs").json()
    assert runs[0]["run_id"] == run_dir.name and runs[0]["moved"] == 2
    assert client.get(f"/v1/runs/{run_dir.name}/report").status_code == 200
    assert client.get("/v1/runs/../../etc").status_code in (400, 404)
    assert client.get("/v1/runs/current").status_code == 404  # pointer absent

    # schedule round-trip
    put = client.put("/v1/schedule", json={"interval": "45min", "enabled": True}).json()
    assert put["interval_s"] == 2700
    assert client.get("/v1/schedule").json()["interval_s"] == 2700

    # 409 while a "runner" is alive (our own pid plays the runner)
    ptr = {"run_id": "r-live", "pid": __import__("os").getpid(), "run_dir": str(run_dir)}
    (tmp_path / "state" / "current-run.json").write_text(json.dumps(ptr))
    (run_dir / "progress.json").write_text(json.dumps(
        {"status": "running", "run_id": "r-live", "started_ts": 1.0}))
    assert client.post("/v1/runs", json={}).status_code == 409
    assert client.get("/v1/runs/current").json()["running"] is True


# --------------------------------------------------------------------------- #
# history.jsonl union-merge                                                   #
# --------------------------------------------------------------------------- #

def _hline(ts, text):
    return json.dumps({"display": text, "timestamp": ts})


def test_union_merge_jsonl_interleaves_and_dedupes():
    from fsync.homesync import union_merge_jsonl
    common = _hline(100, "on both")
    a = "\n".join([common, _hline(300, "a later")]) + "\n"
    b = "\n".join([common, _hline(200, "b middle")]) + "\n"
    merged = union_merge_jsonl(a, b)
    lines = merged.splitlines()
    assert lines == [common, _hline(200, "b middle"), _hline(300, "a later")]
    # idempotent + symmetric-converging: merging the merge changes nothing
    assert union_merge_jsonl(merged, a) == merged
    assert union_merge_jsonl(merged, b) == merged
    assert union_merge_jsonl(b, a) == merged


def test_union_merge_jsonl_edge_cases():
    from fsync.homesync import union_merge_jsonl
    assert union_merge_jsonl("", "") == ""
    only_a = _hline(1, "x") + "\n"
    assert union_merge_jsonl(only_a, "") == only_a
    # malformed lines are preserved (after timestamped ones), never dropped
    bad = "not json at all"
    merged = union_merge_jsonl(_hline(5, "ok") + "\n" + bad + "\n", "")
    assert merged.splitlines() == [_hline(5, "ok"), bad]
    # blank lines vanish, exact duplicates collapse
    assert union_merge_jsonl("\n\n" + only_a, only_a) == only_a


def test_profile_merge_jsonl_config(tmp_path):
    _, profiles, _ = load_config(_write_cfg(tmp_path, """
peer: {host: h}
profiles:
  c: {paths: [.claude], conflict: review, merge_jsonl: ["history.jsonl"]}
  d: {paths: [x]}
"""))
    assert profiles["c"].merge_jsonl == ["history.jsonl"]
    assert profiles["d"].merge_jsonl == []


def test_compare_scales_linearly():
    # regression guard for the O(n^2) matched-list scans: 40k+40k files must
    # compare in seconds, not minutes (the quadratic version needs ~1 min).
    import time as _time
    from fsync.fileindex import compare_file_lists
    n = 40_000
    a = [{"path": f"d/{i}.txt", "name": f"{i}.txt", "hash": f"h{i}", "mtime": 1.0} for i in range(n)]
    b = [{"path": f"d/{i}.txt", "name": f"{i}.txt", "hash": f"h{i}", "mtime": 1.0} for i in range(500, n + 500)]
    t0 = _time.monotonic()
    rep = compare_file_lists(a, b)
    took = _time.monotonic() - t0
    assert len(rep["exact_matches"]) == n - 500
    assert len(rep["only_in_a"]) == 500 and len(rep["only_in_b"]) == 500
    assert took < 5, f"compare took {took:.1f}s — quadratic regression?"
