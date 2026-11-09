"""ISSUE-010: a stale sender working copy resurrects svn-deleted files on the
receiver (docs/issues-and-fixes.md).

Pins the six fixes: ``.svn`` excluded in the starter config (1); the pure
stale-sender guard + its wiring into ``run_profile`` as a sticky, notified skip
(2); resurrection detection against the receiver's previous index (3); the
per-subtree exclude patterns the shipped config uses (4); ``kind: svn``
snapshot/apply against a real ``file://`` repository when svn is installed (5);
the preview's per-checkout parity rows (6).
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fsync import homesync as hs
from fsync import svnsync as svs
from fsync.fileindex import matches_exclude

HAS_SVN = shutil.which("svn") is not None and shutil.which("svnadmin") is not None


# --------------------------------------------------------------------------- #
# fix 1 / fix 4: config patterns                                              #
# --------------------------------------------------------------------------- #

def test_safe_extract_recreates_absolute_symlinks_and_skips_escapes(tmp_path):
    """The unversioned tar may carry a symlink to an absolute path (same home
    layout fleet-wide): it is re-created, not refused; a file whose path escapes
    the working copy is still dropped."""
    import io, os, tarfile
    tar = tmp_path / "u.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        data = b"hello"
        ti = tarfile.TarInfo("sub/plain.txt"); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
        ln = tarfile.TarInfo("links/abs"); ln.type = tarfile.SYMTYPE; ln.linkname = "/var/home/x/configs"
        tf.addfile(ln)
        rel = tarfile.TarInfo("links/rel"); rel.type = tarfile.SYMTYPE; rel.linkname = "../sub/plain.txt"
        tf.addfile(rel)
        esc = tarfile.TarInfo("../escape.txt"); esc.size = 1; tf.addfile(esc, io.BytesIO(b"x"))
    wc = tmp_path / "wc"; wc.mkdir()
    n = svs._safe_extract(tar, wc)
    assert n == 3
    assert (wc / "sub" / "plain.txt").read_text() == "hello"
    assert os.readlink(wc / "links" / "abs") == "/var/home/x/configs"
    assert (wc / "links" / "rel").read_text() == "hello"
    assert not (tmp_path / "escape.txt").exists()
    # idempotent: an existing link is replaced, not an error
    assert svs._safe_extract(tar, wc) == 3


def test_starter_config_excludes_svn_metadata(tmp_path):
    import yaml
    data = yaml.safe_load(hs.DEFAULT_PROFILES_YAML)
    for name in ("homelab", "primary"):
        assert ".svn" in data["profiles"][name]["exclude"], name
    assert matches_exclude("afsas/trunk/.svn/wc.db", [".svn"])
    assert matches_exclude("afsas/trunk/src/.svn/pristine/ab/x.svn-base", [".svn"])
    assert not matches_exclude("afsas/trunk/src/Main.java", [".svn"])


def test_build_output_subtree_patterns_match_what_the_incident_listed():
    pats = ["*.class", ".gradle", "afsas/*/etc/external-libs-builders/*/bin/*",
            "afsas/*/etc/external-libs-builders/*/build/*",
            "afsas/*/test/playwright/build/*", "afsas/*/bin/*"]
    for p in ("afsas/trunk/bin/test/af/X.class", "afsas/trunk/bin/normal/af/Y.class",
              "afsas/trunk/etc/external-libs-builders/javax-xml-ws/.gradle/8.1/x",
              "afsas/trunk/etc/external-libs-builders/afsas-solr-9x-request-handler/build/libs/a.jar",
              "afsas/trunk/etc/external-libs-builders/json-schema-validator-lib/bin/main/A.java",
              "afsas/trunk/test/playwright/build/report.html",
              "afsas/afsas_252/bin/eclipse/af/Z.class"):
        assert matches_exclude(p, pats), p
    for p in ("afsas/trunk/src/main/java/A.java", "afsas/trunk/etc/docs/readme.md",
              "afsas/trunk/test/playwright/tests/login.spec.ts",
              "asap/trunk/bin/run.sh"):   # bin/ is only pruned under afsas/*
        assert not matches_exclude(p, pats), p


# --------------------------------------------------------------------------- #
# fix 2: the pure guard                                                       #
# --------------------------------------------------------------------------- #

def _co(rev, url="svn://srv/repo/trunk", root="svn://srv/repo", **kw):
    return {"rev": rev, "url": url, "root": root, **kw}


def test_guard_skips_sender_behind_receiver_on_push_and_names_the_remedy():
    local = {"afsas/trunk": _co(40260)}
    peer = {"afsas/trunk": _co(40297)}
    g = svs.stale_sender_guard(local, peer, direction="push",
                               local_host="minis4dx.lan", peer_host="fury4dx.lan")
    assert g["checked"] == 1
    assert g["excludes"] == ["afsas/trunk/*"]
    assert len(g["skipped"]) == 1
    reason = g["skipped"][0]["reason"]
    assert "sender r40260 < receiver r40297" in reason
    assert "svn update on minis4dx" in reason
    assert "not file-synced" in reason
    assert g["parity"][0]["state"] == "skip"
    # the exclude pattern really hides the subtree from the file engine
    assert matches_exclude("afsas/trunk/etc/docs/x.md", g["excludes"])
    assert matches_exclude("afsas/trunk/pom.xml", g["excludes"])
    assert not matches_exclude("afsas/oltp/pom.xml", g["excludes"])


def test_guard_direction_semantics():
    local = {"wc": _co(10)}
    peer = {"wc": _co(20)}
    # pull: sender = peer (r20) ahead of receiver = local (r10): advisory only
    g = svs.stale_sender_guard(local, peer, direction="pull", local_host="a", peer_host="b")
    assert not g["skipped"] and len(g["advisory"]) == 1
    assert "svn update on a" in g["advisory"][0]["reason"]
    assert g["parity"][0]["state"] == "advisory"
    # pull with peer behind: skipped
    g = svs.stale_sender_guard({"wc": _co(20)}, {"wc": _co(10)}, direction="pull",
                               local_host="a", peer_host="b")
    assert g["skipped"] and "svn update on b" in g["skipped"][0]["reason"]
    # both: any mismatch skips, the behind side is named
    g = svs.stale_sender_guard(local, peer, direction="both", local_host="a", peer_host="b")
    assert g["skipped"] and "svn update on a" in g["skipped"][0]["reason"]
    # equal: parity
    g = svs.stale_sender_guard({"wc": _co(5)}, {"wc": _co(5)}, direction="push",
                               local_host="a", peer_host="b")
    assert not g["skipped"] and not g["advisory"] and g["parity"][0]["state"] == "ok"


def test_guard_url_mismatch_skips_and_one_sided_checkouts_are_only_listed():
    local = {"x": _co(5, url="svn://srv/repo/branches/b1"), "only-here": _co(1)}
    peer = {"x": _co(5, url="svn://srv/repo/trunk"), "only-there": _co(2)}
    g = svs.stale_sender_guard(local, peer, direction="push", local_host="a", peer_host="b",
                               heads={"svn://srv/repo": 99})
    assert g["checked"] == 1
    assert [s["rel"] for s in g["skipped"]] == ["x"]
    assert "URL differs" in g["skipped"][0]["reason"]
    states = {r["rel"]: r["state"] for r in g["parity"]}
    assert states == {"x": "skip", "only-here": "only-local", "only-there": "only-peer"}
    assert {r["rel"]: r["head_rev"] for r in g["parity"]}["x"] == 99


def test_guard_unknown_revision_is_not_a_skip():
    g = svs.stale_sender_guard({"wc": _co(None, error="svn info failed")}, {"wc": _co(3)},
                               direction="push", local_host="a", peer_host="b")
    assert not g["skipped"]
    assert g["parity"][0]["state"] == "unknown"


def test_parse_probe_tolerates_noise():
    out = ('garbage\n{"rel": "a", "rev": 1, "url": "u", "root": "r"}\n'
           'not json {\n{"heads": {"r": 7}}\n')
    found, heads = svs.parse_probe(out)
    assert found == {"a": {"rel": "a", "rev": 1, "url": "u", "root": "r"}}
    assert heads == {"r": 7}


# --------------------------------------------------------------------------- #
# fix 2 wiring: run_profile skips the subtree, escalates, notifies, sticks    #
# --------------------------------------------------------------------------- #

def _guarded_run(tmp_path, monkeypatch, *, guard, run_id="R1", dry_run=False):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hs, "STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "w"
    root.mkdir(exist_ok=True)
    plan = {"a_to_b": [{"path": "f.txt"}], "b_to_a": [], "a_delete": [], "b_delete": [],
            "conflicts": [], "renames": [], "renames_suppressed": [], "noop": 1}
    seen: dict = {}

    def fake_plan(pe, pr, rel, n):
        seen["exclude"] = list(pr.exclude)
        return root, "/remote/w", plan, set()

    prof = hs.Profile(name="primary", paths=["w"], direction="push", exclude=[".svn"])
    peer = hs.Peer(host="fury4dx.lan", user="dev", home="/remote")
    monkeypatch.setattr(hs, "peer_home", lambda p: "/remote")
    monkeypatch.setattr(hs, "_plan_path", fake_plan)
    monkeypatch.setattr(hs, "run_rsync_leg", lambda *a, **k: {"files": 0, "bytes": 0})
    monkeypatch.setattr(hs, "svn_guard_for_path", lambda *a, **k: dict(guard))
    lines: list[str] = []
    result = hs.run_profile(peer, prof, run_dir=tmp_path / "run" / run_id, run_id=run_id,
                            dry_run=dry_run, workers=1, log=lines.append)
    return result, seen, lines


SKIP_GUARD = {"checked": 1, "excludes": ["afsas/trunk/*"], "advisory": [], "probe_warning": None,
              "skipped": [{"rel": "afsas/trunk", "local_rev": 40260, "peer_rev": 40297,
                           "reason": "afsas/trunk: sender r40260 < receiver r40297 — run svn "
                                     "update on minis4dx, not file-synced"}],
              "parity": [{"rel": "afsas/trunk", "state": "skip"}]}
CLEAN_GUARD = {"checked": 1, "excludes": [], "advisory": [], "skipped": [], "parity": [],
               "probe_warning": None}


def test_run_profile_excludes_stale_subtree_and_escalates(tmp_path, monkeypatch):
    result, seen, lines = _guarded_run(tmp_path, monkeypatch, guard=SKIP_GUARD)
    assert "afsas/trunk/*" in seen["exclude"]          # the subtree never reached the planner
    assert ".svn" in seen["exclude"]                   # on top of the profile's own excludes
    assert result["status"] == "attention"
    d = result["paths"]["w"]
    assert d["svn_guard"]["skipped"][0]["rel"] == "afsas/trunk"
    assert any("SKIPPED afsas/trunk: sender r40260 < receiver r40297" in m for m in lines)
    # first detection is notified right away (not only after 24 runs)
    assert result["warnings"] and "svn update on minis4dx" in result["warnings"][0]
    assert "(ISSUE-010)" in result["warnings"][0]
    st = json.loads((hs.state_root() / "auto-apply-sticky.json").read_text())
    assert st["primary/svn:w/afsas/trunk"]["runs"] == 1


def test_run_profile_sticky_streak_counts_and_clears(tmp_path, monkeypatch):
    for i in range(1, 4):
        result, _, lines = _guarded_run(tmp_path, monkeypatch, guard=SKIP_GUARD, run_id=f"R{i}")
    st = json.loads((hs.state_root() / "auto-apply-sticky.json").read_text())
    assert st["primary/svn:w/afsas/trunk"]["runs"] == 3
    assert st["primary/svn:w/afsas/trunk"]["first_run"] == "R1"
    assert any("[3 in a row since R1]" in m for m in lines)
    assert result["warnings"] == []                    # runs 2..23 stay quiet
    # a dry run reads but never advances
    _guarded_run(tmp_path, monkeypatch, guard=SKIP_GUARD, run_id="D", dry_run=True)
    assert json.loads((hs.state_root() / "auto-apply-sticky.json").read_text())[
        "primary/svn:w/afsas/trunk"]["runs"] == 3
    # svn update happened: parity restored, the streak ends
    result, seen, _ = _guarded_run(tmp_path, monkeypatch, guard=CLEAN_GUARD, run_id="R9")
    assert result["status"] == "ok"
    assert "afsas/trunk/*" not in seen["exclude"]
    assert "primary/svn:w/afsas/trunk" not in json.loads(
        (hs.state_root() / "auto-apply-sticky.json").read_text())


def test_run_profile_probe_failure_is_loud_not_silent(tmp_path, monkeypatch):
    guard = {**CLEAN_GUARD, "probe_warning": "primary/w: svn parity probe failed on dev@fury4dx.lan"}
    result, _, lines = _guarded_run(tmp_path, monkeypatch, guard=guard)
    assert any("probe failed" in w for w in result["warnings"])
    assert any("probe failed" in m for m in lines)


def test_run_report_warnings_reach_the_notification(tmp_path, monkeypatch):
    """_cmd_run forwards file-profile warnings into report['warnings'] (which is
    what --notify reads). Pinned by the sticky git path too — same channel."""
    src = Path(hs.__file__).read_text()
    assert 'report["warnings"].extend(res.get("warnings") or [])' in src


def test_svn_guard_for_path_skips_peer_probe_when_nothing_local(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "_ssh", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ssh used")))
    prof = hs.Profile(name="p", paths=["w"])
    peer = hs.Peer(host="peer", home="/remote")
    (tmp_path / "w").mkdir()
    g = hs.svn_guard_for_path(peer, prof, "w", tmp_path / "w", "/remote/w")
    assert g["checked"] == 0 and g["excludes"] == [] and g["probe_warning"] is None


def test_svn_guard_for_path_reports_dead_peer_probe(tmp_path, monkeypatch):
    prof = hs.Profile(name="p", paths=["w"])
    peer = hs.Peer(host="peer.lan", user="dev", home="/remote")
    monkeypatch.setattr(svs, "probe_local", lambda root, head=False: ({"wc": _co(3)}, {}))
    monkeypatch.setattr(hs, "_ssh", lambda *a, **k: subprocess.CompletedProcess([], 255, "", "down"))
    g = hs.svn_guard_for_path(peer, prof, "w", tmp_path / "w", "/remote/w")
    assert g["probe_warning"] and "guard inactive" in g["probe_warning"]
    assert g["excludes"] == []
    assert g["parity"][0]["state"] == "unknown"


# --------------------------------------------------------------------------- #
# fix 3: resurrection detection                                               #
# --------------------------------------------------------------------------- #

def test_detect_resurrections_against_previous_receiver_index(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "STATE_ROOT", str(tmp_path / "state"))
    # previous run: the peer had etc/docs/a.md; now it does not (svn deleted it there)
    hs._save_prev_index("primary", "w", "peer", {"etc/docs/a.md", "keep.txt"}, "R0")
    plan = {"index_a": {"etc/docs/a.md", "new.txt", "keep.txt"}, "index_b": {"keep.txt"}}
    r = hs.detect_resurrections("primary", "w", plan, ["etc/docs/a.md", "new.txt"], [], set())
    assert r["on_peer"] == ["etc/docs/a.md"]      # had it, deleted it, we'd bring it back
    assert r["count"] == 1 and r["baseline"] == {"peer": True, "local": False}


def test_detect_resurrections_ignores_overwrites_and_needs_a_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "STATE_ROOT", str(tmp_path / "state"))
    plan = {"index_a": {"a"}, "index_b": set()}
    assert hs.detect_resurrections("p", "w", plan, ["a"], [], set())["count"] == 0  # no baseline
    hs._save_prev_index("p", "w", "peer", {"a"}, "R0")
    # an overwrite means the receiver still has the path: not a resurrection
    plan = {"index_a": {"a"}, "index_b": {"a"}}
    assert hs.detect_resurrections("p", "w", plan, ["a"], [], {"a"})["count"] == 0
    # older plan shape (no index sets): detection off, never crashes
    assert hs.detect_resurrections("p", "w", {}, ["a"], [], set())["count"] == 0


def test_run_profile_reports_resurrections_and_saves_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(hs, "STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "w"
    root.mkdir()
    hs._save_prev_index("primary", "w", "peer", {f"ghost-{i}.txt" for i in range(30)}, "R0")
    plan = {"a_to_b": [{"path": f"ghost-{i}.txt"} for i in range(30)] + [{"path": "fresh.txt"}],
            "b_to_a": [], "a_delete": [], "b_delete": [], "conflicts": [], "renames": [],
            "renames_suppressed": [], "noop": 0,
            "index_a": {f"ghost-{i}.txt" for i in range(30)} | {"fresh.txt"}, "index_b": set()}
    prof = hs.Profile(name="primary", paths=["w"], direction="push")
    peer = hs.Peer(host="peer.lan", user="dev", home="/remote")
    monkeypatch.setattr(hs, "peer_home", lambda p: "/remote")
    monkeypatch.setattr(hs, "_plan_path", lambda pe, pr, rel, n: (root, "/remote/w", plan, set()))
    monkeypatch.setattr(hs, "run_rsync_leg", lambda *a, **k: {"files": 0, "bytes": 0})
    monkeypatch.setattr(hs, "svn_guard_for_path", lambda *a, **k: dict(CLEAN_GUARD))
    lines: list[str] = []
    result = hs.run_profile(peer, prof, run_dir=tmp_path / "run", run_id="R1",
                            dry_run=False, workers=1, log=lines.append)
    d = result["paths"]["w"]["resurrected"]
    assert d["count"] == 30 and d["truncated"] is True
    assert len(d["on_peer"]) == hs.RESURRECTED_SAMPLE
    assert "fresh.txt" not in d["on_peer"]
    assert result["status"] == "attention"
    assert any("RESURRECTED" in m and "undo" in m for m in lines)
    assert result["warnings"] and "RESURRECTED" in result["warnings"][0]
    lst = Path(d["list"]).read_text().splitlines()
    assert len(lst) == 30 and lst[0].startswith("peer\t")
    # the baseline for the next run is this run's receiver index
    with gzip.open(hs._prev_index_path("primary", "w", "peer"), "rt") as f:
        assert json.load(f)["paths"] == []
    with gzip.open(hs._prev_index_path("primary", "w", "local"), "rt") as f:
        assert "fresh.txt" in json.load(f)["paths"]


# --------------------------------------------------------------------------- #
# shared sticky streaks: the git path keeps its exact wording                  #
# --------------------------------------------------------------------------- #

def test_sticky_streaks_git_wording_and_escalation(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "STATE_ROOT", str(tmp_path / "state"))
    for i in range(1, hs.AUTO_APPLY_STICKY_RUNS + 1):
        s = hs.StickyStreaks("repos/", run_id=f"r{i}", dry_run=False)
        runs, first, suffix = s.note("proj", "receiver has local changes")
        s.finish()
    assert runs == hs.AUTO_APPLY_STICKY_RUNS and first == "r1"
    assert s.sticky and s.sticky[0]["repo"] == "proj"
    assert s.warnings == [f"repos/proj: auto-apply skipped {hs.AUTO_APPLY_STICKY_RUNS} runs in a "
                          f"row since r1 (receiver has local changes) — receiver work is stuck, "
                          f"reconcile by hand (ISSUE-006)"]


# --------------------------------------------------------------------------- #
# fix 6: preview parity rows                                                   #
# --------------------------------------------------------------------------- #

def test_plan_screen_shows_stale_checkouts_only():
    from dream4devops.ui_lite.render_web import to_html
    from fsync import views
    plan = {"peer": "dev@peer", "profiles": {"primary": {
        "paths": {"workspaces/primary": {
            "a_to_b": 3, "b_to_a": 0, "bytes_a_to_b": 10, "bytes_b_to_a": 0,
            "overwrites_a_to_b": 0, "overwrites_b_to_a": 0, "conflicts": 0, "identical": 5,
            "renames_demoted": 0, "svn_skipped": 1, "svn_checked": 3,
            "samples": {"a_to_b": [], "b_to_a": []}}},
        "svn": [
            {"path": "workspaces/primary", "rel": "afsas/trunk", "state": "skip",
             "local_rev": 40260, "peer_rev": 40297, "head_rev": 40300,
             "reason": "afsas/trunk: sender r40260 < receiver r40297 — run svn update on minis4dx, not file-synced"},
            {"path": "workspaces/primary", "rel": "asap/trunk", "state": "ok",
             "local_rev": 2722, "peer_rev": 2722, "head_rev": None, "reason": None},
            {"path": "workspaces/primary", "rel": "solr", "state": "advisory",
             "local_rev": 3067, "peer_rev": 2982, "head_rev": None, "reason": "solr: sender r3067 > receiver r2982 — run svn update on fury4dx (receiver is stale)"},
            {"path": "workspaces/primary", "rel": "tomcat-11", "state": "only-peer",
             "local_rev": None, "peer_rev": 3062, "head_rev": None, "reason": None},
        ]}}}
    html = to_html(views.plan_screen(plan, {"primary": "push"}, dry=False))
    assert "afsas/trunk" in html and "SKIPPED" in html
    assert "r40260" in html and "r40297" in html and "HEAD r40300" in html
    assert "solr" in html and "receiver stale" in html
    assert "asap/trunk" not in html                     # in parity: summarised only
    assert "1/3 checkout(s) in revision parity" in html and "1 one-sided" in html
    assert "will NOT be file-synced" in html
    # the PLAN fixture without svn data renders exactly as before
    plain = {"peer": "p", "profiles": {"d": {"paths": {"x": {
        "a_to_b": 0, "b_to_a": 0, "bytes_a_to_b": 0, "bytes_b_to_a": 0, "overwrites_a_to_b": 0,
        "overwrites_b_to_a": 0, "conflicts": 0, "identical": 1, "renames_demoted": 0,
        "samples": {"a_to_b": [], "b_to_a": []}}}}}}
    assert "parity" not in to_html(views.plan_screen(plain, {"d": "both"}, dry=False))


# --------------------------------------------------------------------------- #
# fix 5: kind: svn against a real file:// repository                           #
# --------------------------------------------------------------------------- #

def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def svn_world(tmp_path):
    """A repo with trunk at r3, a producer checkout (r3, one modified file, one
    unversioned file) and a receiver checkout stuck at r1 (before a delete)."""
    repo = tmp_path / "repo"
    _run(["svnadmin", "create", str(repo)], tmp_path)
    url = f"file://{repo}/trunk"
    _run(["svn", "mkdir", "-q", "-m", "init", "--parents", url], tmp_path)
    prod = tmp_path / "home-a" / "ws" / "proj"
    recv = tmp_path / "home-b" / "ws" / "proj"
    prod.parent.mkdir(parents=True)
    recv.parent.mkdir(parents=True)
    _run(["svn", "co", "-q", url, str(prod)], tmp_path)
    (prod / "keep.txt").write_text("keep\n")
    (prod / "gone.txt").write_text("gone\n")
    _run(["svn", "add", "-q", "keep.txt", "gone.txt"], prod)
    _run(["svn", "ci", "-q", "-m", "r2 add"], prod)            # r2
    _run(["svn", "co", "-q", str(url) + "@2", str(recv)], tmp_path)
    _run(["svn", "rm", "-q", "gone.txt"], prod)
    _run(["svn", "ci", "-q", "-m", "r3 delete"], prod)         # r3
    _run(["svn", "up", "-q"], prod)
    (prod / "keep.txt").write_text("keep\nedited on producer\n")
    (prod / "scratch.txt").write_text("unversioned\n")
    return {"url": url, "prod": prod, "recv": recv, "home_a": tmp_path / "home-a",
            "home_b": tmp_path / "home-b"}


@pytest.mark.skipif(not HAS_SVN, reason="svn/svnadmin not installed")
def test_probe_and_guard_end_to_end(svn_world):
    local, _ = svs.probe_local(svn_world["home_a"] / "ws")
    peer, _ = svs.probe_local(svn_world["home_b"] / "ws")
    assert local["proj"]["rev"] == 3 and peer["proj"]["rev"] == 2
    assert local["proj"]["url"] == svn_world["url"]
    # the receiver (r2) pushing to the producer (r3) would resurrect gone.txt: skipped
    g = svs.stale_sender_guard(peer, local, direction="push", local_host="b", peer_host="a")
    assert g["skipped"] and "sender r2 < receiver r3" in g["skipped"][0]["reason"]
    # the runner-based peer probe produces the same map through a shell
    def runner(cmd, timeout):
        return subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    remote = svs.probe_remote(runner, str(svn_world["home_b"] / "ws"))
    assert remote is not None and remote[0]["proj"]["rev"] == 2
    assert svs.probe_remote(lambda cmd, timeout: subprocess.CompletedProcess([], 1, "", "x"), "/x") is None


@pytest.mark.skipif(not HAS_SVN, reason="svn/svnadmin not installed")
def test_kind_svn_snapshot_then_guarded_apply_then_restore(svn_world, tmp_path):
    snap = tmp_path / "snaps"
    meta = svs.snapshot_wc(svn_world["prod"], snap, name="ws__proj", rel="ws/proj")
    assert meta["revision"] == 3 and meta["dirty"] is True
    assert meta["unversioned"] == ["scratch.txt"] and meta["unversioned_tar"]
    assert "edited on producer" in (snap / meta["diff"]).read_text()
    # unchanged unversioned set: the tar is reused, the id is stable
    meta2 = svs.snapshot_wc(svn_world["prod"], snap, name="ws__proj", rel="ws/proj")
    assert meta2["snapshot_id"] == meta["snapshot_id"]

    recv = svn_world["recv"]
    state = tmp_path / "state"
    ok, reason = svs.apply_decision(recv, meta, state_dir=state)
    assert (ok, reason) == (True, "clean")
    # a dirty receiver is never clobbered
    (recv / "keep.txt").write_text("receiver edit\n")
    assert svs.apply_decision(recv, meta, state_dir=state) == (False, "receiver has local changes")
    _run(["svn", "revert", "-q", "keep.txt"], recv)
    # receiver ahead of the snapshot: stale snapshot, skip
    assert svs.apply_decision(recv, {**meta, "revision": 1}, state_dir=state)[1].startswith("receiver is ahead")
    # wrong URL at the same path: skip
    assert "URL differs" in svs.apply_decision(recv, {**meta, "url": "file:///elsewhere"}, state_dir=state)[1]

    backup = tmp_path / "bk" / "run1-svnapply"
    res = svs.apply_wc(recv, snap, meta, backup_dir=backup, state_dir=state)
    assert res["revision"] == 3 and res["patched"] and res["unpacked"] == 1
    assert not (recv / "gone.txt").exists()                  # update brought the delete
    assert "edited on producer" in (recv / "keep.txt").read_text()
    assert (recv / "scratch.txt").read_text() == "unversioned\n"
    assert json.loads((backup / "ws__proj.pre-apply.meta.json").read_text())["revision"] == 2
    # idempotent: the same snapshot is "up to date" now
    _run(["svn", "revert", "-q", "-R", "."], recv)            # receiver clean again, r3
    (recv / "scratch.txt").unlink()
    assert svs.apply_decision(recv, meta, state_dir=state) == (False, "up to date")
    # restore: back to r2, gone.txt is back
    r = svs.restore_backup(recv, backup, "ws__proj")
    assert r["revision"] == 2 and (recv / "gone.txt").exists()
    # a fresh checkout is created for a receiver that has nothing there yet
    new = tmp_path / "home-c" / "ws" / "proj"
    assert svs.apply_decision(new, meta) == (True, "new")
    res = svs.apply_wc(new, snap, meta, backup_dir=tmp_path / "bk" / "run2-svnapply")
    assert res["created"] and svs.wc_info(new)["revision"] == 3


@pytest.mark.skipif(not HAS_SVN, reason="svn/svnadmin not installed")
def test_kind_svn_profile_snapshot_and_auto_apply_through_homesync(svn_world, tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "STATE_ROOT", str(tmp_path / "state"))
    snap = tmp_path / "snaps"
    prof = hs.Profile(name="svn", paths=["ws"], kind="svn", direction="push")
    monkeypatch.setattr(hs, "_home", lambda: svn_world["home_a"])
    lines: list[str] = []
    out = hs.snapshot_svn_profile(prof, snap, lines.append)
    assert out["checkouts"][0]["rel"] == "ws/proj" and out["checkouts"][0]["revision"] == 3
    # receiver side
    monkeypatch.setattr(hs, "_home", lambda: svn_world["home_b"])
    rprof = hs.Profile(name="svn", paths=["ws"], kind="svn", direction="pull", apply="auto")
    assert hs.auto_apply_svn_checkouts(rprof) and not hs.svn_profile_snapshots(rprof)
    dry = hs.auto_apply_svn_profile(rprof, snap, run_id="R1", dry_run=True, log=lines.append)
    assert dry["applied"] == [{"checkout": "proj", "dry_run": True}]
    assert svs.wc_info(svn_world["recv"])["revision"] == 2        # dry run touched nothing
    res = hs.auto_apply_svn_profile(rprof, snap, run_id="R2", dry_run=False, log=lines.append)
    assert res["status"] == "ok" and res["applied"][0]["revision"] == 3
    assert svs.wc_info(svn_world["recv"])["revision"] == 3
    # file profiles skip an apply:auto svn tree, like git fix C
    fprof = hs.Profile(name="files", paths=["ws"])
    ex = hs.git_repo_excludes_for_file_profiles({"svn": rprof, "files": fprof})
    assert ex == {"files": ["proj/*"]}


def test_profile_kind_svn_loads_and_requires_feature(tmp_path):
    cfg = tmp_path / "p.yaml"
    cfg.write_text("peer: {host: h}\nprofiles:\n  svn:\n    kind: svn\n    paths: [ws]\n"
                   "  bad:\n    kind: hg\n    paths: [x]\n")
    with pytest.raises(hs.HomesyncError, match="kind must be"):
        hs.load_config(str(cfg))
    cfg.write_text("peer: {host: h}\nprofiles:\n  svn:\n    kind: svn\n    paths: [ws]\n")
    _, profiles, _ = hs.load_config(str(cfg))
    assert profiles["svn"].kind == "svn"
    from fsync import FEATURES
    assert "svn-sync" in FEATURES
    from fsync import meta_deploy
    assert "svnsync.py" in meta_deploy.CORE_MODULES
