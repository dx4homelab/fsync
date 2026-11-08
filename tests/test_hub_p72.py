"""P7.2 hub-sync state layer (docs/hub-sync.md R32–R35): classifier table,
hub paths/journal/commit/index/gc, holds, and the acceptance scenario —
three homes converging through one hub on this box with deletes, revivals,
both-changed, an "away" laptop replaying once, and the mass-delete guard."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import time
from pathlib import Path

import pytest

from fsync import homesync as hs
from fsync import threeway as tw
from fsync.homesync import HomesyncError, load_config
from fsync.hubstate import HubState, LeaseLost, LocalHubControl

HOST = socket.gethostname().split(".")[0]


# --------------------------------------------------------------------------- #
# classifier                                                                  #
# --------------------------------------------------------------------------- #

def L(size=1, mtime=100.0, h="h"):
    return {"size": size, "mtime": mtime, "hash": h}


def H(size=1, mtime=100.0, h="h", seq=1):
    return [size, mtime, h, seq]


def B(size=1, mtime=100.0, h="h"):
    return [size, mtime, h]


def test_classify_table():
    # base present, unchanged both sides → noop
    p = tw.classify({"a": B()}, {"a": L()}, {"a": H()}, {})
    assert p.noop == 1 and not p.push and not p.pull
    # local changed only → push (overwrite)
    p = tw.classify({"a": B()}, {"a": L(h="h2", mtime=200)}, {"a": H()}, {})
    assert p.push == ["a"] and p.overwrite_push == ["a"]
    # hub changed only → pull
    p = tw.classify({"a": B()}, {"a": L()}, {"a": H(h="h2", mtime=200)}, {})
    assert p.pull == ["a"] and p.overwrite_pull == ["a"]
    # local deleted, hub unchanged → tombstone on hub
    p = tw.classify({"a": B()}, {}, {"a": H()}, {})
    assert p.del_hub == ["a"]
    # hub tombstone, local unchanged → remove locally (backup)
    p = tw.classify({"a": B()}, {"a": L()}, {}, {"a": [150.0, 2]})
    assert p.del_local == ["a"]
    # hub tombstone, local edited AFTER the delete → revive
    p = tw.classify({"a": B()}, {"a": L(h="h2", mtime=300)}, {}, {"a": [150.0, 2]})
    assert p.push == ["a"] and p.revived == ["a"]
    # hub tombstone, local edited BEFORE the delete → delete wins
    p = tw.classify({"a": B()}, {"a": L(h="h2", mtime=120)}, {}, {"a": [150.0, 2]})
    assert p.del_local == ["a"]
    # both changed, same content → noop
    p = tw.classify({"a": B()}, {"a": L(h="x")}, {"a": H(h="x")}, {})
    assert p.noop == 1
    # both changed, newer local wins / newer hub wins
    p = tw.classify({"a": B()}, {"a": L(h="l", mtime=300)}, {"a": H(h="r", mtime=200)}, {})
    assert p.push == ["a"]
    p = tw.classify({"a": B()}, {"a": L(h="l", mtime=100)}, {"a": H(h="r", mtime=200)}, {})
    assert p.pull == ["a"]
    # both changed under review → held
    p = tw.classify({"a": B()}, {"a": L(h="l", mtime=300)}, {"a": H(h="r")}, {}, conflict="review")
    assert [c["path"] for c in p.conflicts] == ["a"] and not p.push and not p.pull
    # both changed merge_jsonl → merge
    p = tw.classify({"h.jsonl": B()}, {"h.jsonl": L(h="l")}, {"h.jsonl": H(h="r")}, {},
                    merge_jsonl=["history.jsonl", "h.jsonl"])
    assert p.merge == ["h.jsonl"]
    # local deleted but hub edited since → edit beats delete (pull back)
    p = tw.classify({"a": B()}, {}, {"a": H(h="h2", mtime=200)}, {})
    assert p.pull == ["a"]
    # both deleted → noop
    p = tw.classify({"a": B()}, {}, {}, {"a": [150.0, 2]})
    assert p.noop == 1


def test_classify_first_run():
    # no base: local only → push + introduction; hub only → pull; same → noop; differ → newest
    p = tw.classify(None, {"l": L(), "s": L(h="s"), "d": L(h="d1", mtime=300)},
                    {"h": H(), "s": H(h="s"), "d": H(h="d2", mtime=100)}, {})
    assert p.push == ["d", "l"] and p.introductions == ["l"] and p.pull == ["h"] and p.noop == 1
    # no base, hub tombstone newer than our file → remove; older → revive
    p = tw.classify(None, {"a": L(mtime=100), "b": L(mtime=300)}, {}, {"a": [150.0, 1], "b": [150.0, 2]})
    assert p.del_local == ["a"] and p.push == ["b"] and p.revived == ["b"]


def test_direction_and_mass_delete_guard():
    p = tw.classify({"a": B(), "b": B()}, {"a": L(h="x")}, {"a": H(), "b": H(), "c": H()}, {})
    assert p.push == ["a"] and p.del_hub == ["b"] and p.pull == ["c"]
    tw.apply_direction(p, "pull")
    assert p.push == [] and p.del_hub == [] and p.stranded_push == ["a"] and p.stranded_del_hub == ["b"]
    assert p.pull == ["c"]
    base = {f"f{i}": B() for i in range(1000)}
    hub = {k: H() for k in base}
    local = {f"f{i}": L() for i in range(300)}   # 700 "deleted"
    p = tw.guard_mass_delete(tw.classify(base, local, hub, {}), len(base))
    assert p.del_hub == [] and len(p.mass_delete_held) == 700
    p = tw.guard_mass_delete(tw.classify(base, local, hub, {}), len(base), allow=True)
    assert len(p.del_hub) == 700
    # a handful of deletes passes
    local = {f"f{i}": L() for i in range(990)}
    p = tw.guard_mass_delete(tw.classify(base, local, hub, {}), len(base))
    assert len(p.del_hub) == 10


def test_next_base_keeps_unconverged_entries():
    live = {"a": H(h="ha"), "b": H(h="hb"), "c": H(h="hc")}
    local = {"a": L(h="la"), "d": L(h="ld")}
    old = {"a": B(h="old-a"), "b": B(h="old-b"), "c": B(h="old-c")}
    view, ov = tw.next_base(live, local, old, accepted_puts={"d"}, rejected_puts={"a"}, accepted_dels=set(),
                            rejected_dels=set(), pulled_ok={"c"}, pulled_failed={"b"}, deleted_local=set(),
                            unresolved=set())
    assert view["d"][2] == "ld"                      # accepted put = local content
    assert view["a"][2] == "ha" and ov["a"][2] == "old-a"   # rejected put: view exact, converged = old
    assert view["b"][2] == "hb" and ov["b"][2] == "old-b"   # failed pull likewise
    assert view["c"][2] == "hc" and "c" not in ov   # verified pull converged
    cb = tw.converged_base({"seq": 1, "paths": view, "overrides": ov})
    assert cb["a"][2] == "old-a" and cb["c"][2] == "hc"
    # a path we never had that failed to converge → override None → classified as new again
    view2, ov2 = tw.next_base({"n": H(h="hn")}, {}, {}, accepted_puts=set(), rejected_puts=set(),
                              accepted_dels=set(), rejected_dels=set(), pulled_ok=set(), pulled_failed={"n"},
                              deleted_local=set(), unresolved=set())
    assert ov2 == {"n": None} and "n" not in tw.converged_base({"seq": 1, "paths": view2, "overrides": ov2})


def test_journal_replay_and_prefix():
    live, tomb = tw.hub_view_from_base({"seq": 5, "paths": {"x": B()}})
    tw.apply_journal(live, tomb, [[6, "Documents/y", "put", "hy", 2, 7.0, "fury4dx", 1.0],
                                  [7, "Documents/x", "del", None, None, None, "omen4dx", 2.0],
                                  [8, "Pictures/z", "put", "hz", 1, 1.0, "minis4dx", 3.0]],
                     strip_prefix="Documents")
    assert live == {"y": [2, 7.0, "hy", 6]} and tomb == {"x": [2.0, 7]}
    live2, tomb2 = tw.strip_state_prefix({"paths": {"Documents/a": H()}, "tombstones": {"Pictures/b": [1.0, 1]}},
                                         "Documents")
    assert live2 == {"a": H()} and tomb2 == {}
    assert tw.hub_path("Documents", "a") == "Documents/a" and tw.hub_path(".", "a") == "a"


# --------------------------------------------------------------------------- #
# hub state: commit / index / journal / gc / holds                            #
# --------------------------------------------------------------------------- #

def _tree(tmp_path: Path, profile: str = "docs") -> tuple[HubState, Path, Path]:
    root = tmp_path / "hub"
    tree = root / profile / "tree"
    tree.mkdir(parents=True)
    return HubState(root / "state.sqlite"), root, tree


def test_commit_verifies_puts_and_tombstones_dels(tmp_path):
    st, root, tree = _tree(tmp_path)
    (tree / "Documents").mkdir()
    (tree / "Documents" / "a.txt").write_text("A")
    (tree / "Documents" / "gone.txt").write_text("G")
    sa = (tree / "Documents" / "a.txt").stat()
    lease = st.acquire("docs", "fury4dx", "R1", ttl=600)
    res = st.commit("docs", "fury4dx", "R1", lease["lease_id"],
                    puts=[{"path": "Documents/a.txt", "size": sa.st_size, "mtime": sa.st_mtime, "hash": "ha"},
                          {"path": "Documents/missing.txt", "size": 1, "mtime": 1.0, "hash": "x"},
                          {"path": "Documents/a.txt", "size": 999, "mtime": sa.st_mtime, "hash": "ha"},
                          {"path": "../evil", "size": 1, "mtime": 1.0}],
                    dels=[{"path": "Documents/gone.txt", "hash": "hg"}, {"path": "Documents/never.txt"}],
                    holds=[{"path": "Documents/c.txt", "hub_hash": "h1", "incoming_hash": "h2", "incoming_mtime": 5.0}],
                    tree_root=tree, backup_dir=root / "backups" / "R1" / "docs")
    assert res["accepted_puts"] == ["Documents/a.txt", "Documents/a.txt"] or res["accepted_puts"] == ["Documents/a.txt"]
    reasons = {r["path"]: r["reason"] for r in res["rejected"]}
    assert "not in hub tree" in reasons["Documents/missing.txt"]
    assert reasons["../evil"] == "bad path"
    assert any("size" in r["reason"] for r in res["rejected"] if r["path"] == "Documents/a.txt")
    assert res["accepted_dels"] == ["Documents/gone.txt", "Documents/never.txt"]
    assert not (tree / "Documents" / "gone.txt").exists()
    assert (root / "backups" / "R1" / "docs" / "Documents" / "gone.txt").read_text() == "G"
    state = st.state("docs", prefix="Documents")
    assert state["paths"]["Documents/a.txt"][2] == "ha"
    assert set(state["tombstones"]) == {"Documents/gone.txt", "Documents/never.txt"}
    assert res["seq_to"] == state["seq"] >= 3
    assert st.holds("docs")[0]["path"] == "Documents/c.txt"
    assert st.cursors()["docs"]["fury4dx"]["last_seq"] == res["seq_to"]
    # a put on a held path clears the hold; a stale lease is refused
    (tree / "Documents" / "c.txt").write_text("C")
    sc = (tree / "Documents" / "c.txt").stat()
    st.commit("docs", "omen4dx", "R2", None, [{"path": "Documents/c.txt", "size": sc.st_size,
                                                "mtime": sc.st_mtime, "hash": "hc"}], [], [],
              tree_root=tree, backup_dir=root / "backups" / "R2" / "docs")
    assert st.holds("docs") == []
    with pytest.raises(LeaseLost):
        st.commit("docs", "fury4dx", "R3", "deadbeef", [], [], [], tree_root=tree,
                  backup_dir=root / "backups" / "R3" / "docs")
    # journal since
    j = st.journal_since("docs", 0, prefix="Documents")
    ops = [(e[1], e[2]) for e in j["entries"]]
    assert ("Documents/a.txt", "put") in ops and ("Documents/gone.txt", "del") in ops
    assert ("Documents/c.txt", "hold") in ops and j["complete"] is True
    assert st.journal_since("docs", j["seq"])["entries"] == []


def test_index_tree_bootstrap_repair_and_gc(tmp_path):
    st, root, tree = _tree(tmp_path)
    (tree / "a").write_text("A"); (tree / "b").write_text("B")
    assert not st.is_indexed("docs")
    recs = hs.local_index(tree, recursive=True, exclude=[], workers=1)
    r1 = st.index_tree("docs", recs, now=1000.0)
    assert (r1["added"], r1["tombstoned"]) == (2, 0) and st.is_indexed("docs")
    # file vanished from the tree + file changed → repair
    (tree / "b").unlink(); (tree / "a").write_text("AA")
    recs = hs.local_index(tree, recursive=True, exclude=[], workers=1)
    r2 = st.index_tree("docs", recs, now=2000.0)
    assert (r2["changed"], r2["tombstoned"], r2["unchanged"]) == (1, 1, 0)
    s = st.state("docs")
    assert "b" in s["tombstones"] and s["paths"]["a"][0] == 2
    # gc: tombstone older than retention goes; journal floor makes old cursors fall back
    now = 2000.0 + 100 * 86400
    dry = st.gc(tombstone_days=90, journal_days=90, now=now, dry_run=True)
    assert dry["tombstones"] == 1 and dry["journal"] >= 3
    st.gc(tombstone_days=90, journal_days=90, now=now)
    assert st.state("docs")["tombstones"] == {}
    assert st.journal_since("docs", 0)["complete"] is False
    assert st.journal_since("docs", st.journal_floor("docs"))["complete"] is True


def test_mode_defaults_and_validation(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"""
version: 2
hub: {{host: {HOST}, root: {tmp_path / 'hub'}}}
hosts: {{{HOST}: {{role: [hub, spoke]}}}}
shared:
  profiles:
    a: {{paths: [A]}}
    b: {{paths: [B], mode: additive}}
""")
    _, profiles, _ = load_config(str(cfg))
    assert profiles["a"].mode == "full" and profiles["b"].mode == "additive"
    v1 = tmp_path / "v1.yaml"
    v1.write_text("peer: {host: h}\nprofiles: {x: {paths: [a]}}")
    _, p1, _ = load_config(str(v1))
    assert p1["x"].mode == "additive"
    bad = tmp_path / "bad.yaml"
    bad.write_text("peer: {host: h}\nprofiles: {x: {paths: [a], mode: full}}")
    with pytest.raises(HomesyncError, match="needs a hub"):
        load_config(str(bad))
    from fsync import FEATURES
    assert "hub-full" in FEATURES


# --------------------------------------------------------------------------- #
# acceptance: three homes, one local hub, mode: full                          #
# --------------------------------------------------------------------------- #

pytestmark_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")


class Fleet:
    """Three homes + a hub on this box. ``run(home)`` = `fsync sync run --all` as that box."""

    def __init__(self, tmp_path: Path, monkeypatch, *, conflict="newer", directions=None,
                 profiles="    docs: {paths: [Documents]}\n"):
        self.tmp, self.mp = tmp_path, monkeypatch
        self.hub_root = tmp_path / "hub"
        dirs = directions or {}
        self.cfg = tmp_path / "sync-profiles.yaml"
        self.cfg.write_text(f"""
version: 2
requires: [hub-sync, hub-full]
hub: {{host: {HOST}, root: {self.hub_root}}}
hosts:
  {HOST}: {{role: [hub, spoke]}}
shared:
  defaults: {{conflict: {conflict}}}
  profiles:
{profiles}""")
        self.homes = {n: tmp_path / f"home-{n}" for n in "ABC"}
        for h in self.homes.values():
            (h / "Documents").mkdir(parents=True)
        self.be("A")
        from fsync.hub import hub_init
        hub, profiles_, _ = load_config(str(self.cfg))
        hub_init(hub, profiles_)
        self.state = HubState(self.hub_root / "state.sqlite")

    def be(self, name: str) -> Path:
        home = self.homes[name]
        self.mp.setenv("HOME", str(home))
        self.mp.setattr(hs, "STATE_ROOT", str(home / ".local" / "state" / "fsync"))
        return home

    def run(self, name: str, expect_rc: int = 0, **kw) -> dict:
        self.be(name)
        args = dict(all=True, profile=None, dry_run=False, workers=1, notify=False, plan_only=False,
                    config=str(self.cfg), allow_mass_delete=False)
        args.update(kw)
        rc = hs._cmd_run(argparse.Namespace(**args))
        assert rc == expect_rc, f"run on {name} rc={rc}"
        rid = (hs.state_root() / "runs" / "latest").read_text().strip()
        return json.loads((hs.state_root() / "runs" / rid / "report.json").read_text())

    def doc(self, name: str, rel: str) -> Path:
        return self.homes[name] / "Documents" / rel

    def backups(self, name: str, rel: str) -> list[Path]:
        return list((self.homes[name] / ".fsync" / "backups").rglob(rel))

    def hub_tree(self, rel: str) -> Path:
        return self.hub_root / "docs" / "tree" / "Documents" / rel


def _docs(rep: dict) -> dict:
    return rep["profiles"]["docs"]["paths"]["Documents"]


@pytestmark_rsync
def test_three_homes_delete_revive_conflict_and_away(tmp_path, monkeypatch):
    f = Fleet(tmp_path, monkeypatch)
    f.doc("A", "keep.txt").write_text("keep")
    f.doc("A", "gone.txt").write_text("gone")
    r = f.run("A")
    d = _docs(r)
    assert d["mode"] == "full" and d["first_run"] is True and d["a_to_b"]["files_transferred"] == 2
    assert d["introductions"] == 2
    assert f.hub_tree("gone.txt").exists()
    for n in "BC":
        d = _docs(f.run(n))
        assert d["b_to_a"]["files_transferred"] == 2 and f.doc(n, "gone.txt").read_text() == "gone"

    # delete on A → tombstone on the hub → gone on B and C, present in their backup dirs
    f.doc("A", "gone.txt").unlink()
    d = _docs(f.run("A"))
    assert d["deleted_hub"] == ["gone.txt"] and not f.hub_tree("gone.txt").exists()
    assert list((f.hub_root / "backups").rglob("gone.txt"))  # hub copy kept
    for n in "BC":
        d = _docs(f.run(n))
        assert d["deleted_local"] == ["gone.txt"]
        assert not f.doc(n, "gone.txt").exists()
        assert len(f.backups(n, "gone.txt")) == 1
    # the tombstone is remembered on the hub, nobody re-pulls it
    st = f.state.state("docs")
    assert "Documents/gone.txt" in st["tombstones"]
    assert _docs(f.run("A"))["b_to_a"]["files_transferred"] == 0

    # re-create on B after the delete → present everywhere again (revived)
    time.sleep(1.1)
    f.doc("B", "gone.txt").write_text("back")
    d = _docs(f.run("B"))
    assert d["revived"] == ["gone.txt"] and f.hub_tree("gone.txt").read_text() == "back"
    for n in "AC":
        f.run(n)
        assert f.doc(n, "gone.txt").read_text() == "back"

    # both-changed → newer wins, loser backed up on exactly one box
    f.doc("A", "keep.txt").write_text("from A older")
    time.sleep(1.1)
    f.doc("C", "keep.txt").write_text("from C newer")
    f.run("A")                              # A pushes its version first
    d = _docs(f.run("C"))                   # C's is newer → C wins, hub copy → hub backup
    assert d["a_to_b"]["files_transferred"] == 1 and d["a_to_b"]["overwrites_expected"] == 1
    assert f.hub_tree("keep.txt").read_text() == "from C newer"
    d = _docs(f.run("A"))                   # A's older copy is overwritten, backed up on A
    assert f.doc("A", "keep.txt").read_text() == "from C newer"
    assert len(f.backups("A", "keep.txt")) == 1 and f.backups("A", "keep.txt")[0].read_text() == "from A older"
    assert f.backups("C", "keep.txt") == [] and f.doc("C", "keep.txt").read_text() == "from C newer"

    # laptop "away": C skips while A makes 50 changes (and B deletes one) → one run replays all
    for i in range(50):
        f.doc("A", f"away-{i}.txt").write_text(str(i))
    f.run("A")
    f.run("B")
    f.doc("B", "away-7.txt").unlink()
    f.run("B")
    before = f.state.head_seq("docs")
    d = _docs(f.run("C"))
    assert d["b_to_a"]["files_transferred"] == 49 and d["deleted_local"] == []   # never had away-7
    assert not f.doc("C", "away-7.txt").exists() and f.doc("C", "away-49.txt").exists()
    assert d["seq"] == before and d["base_seq"] < before
    base = tw.load_base(tw.base_path("docs", "Documents", home=f.homes["C"]))
    assert base["seq"] == before and "away-49.txt" in base["paths"] and "away-7.txt" not in base["paths"]
    # a second C run is a pure no-op and advances nothing
    d2 = _docs(f.run("C"))
    assert d2["a_to_b"]["files_transferred"] == 0 and d2["b_to_a"]["files_transferred"] == 0
    assert d2["seq"] == before
    assert f.state.status()["leases"] == {}


@pytestmark_rsync
def test_pull_only_spoke_never_pushes_or_tombstones(tmp_path, monkeypatch):
    f = Fleet(tmp_path, monkeypatch, profiles="    docs: {paths: [Documents], direction: pull}\n")
    # seed the hub directly as a producer would
    f.hub_tree("x.txt").parent.mkdir(parents=True, exist_ok=True)
    f.hub_tree("x.txt").write_text("x")
    f.state.index_tree("docs", hs.local_index(f.hub_root / "docs" / "tree", recursive=True, exclude=[], workers=1))
    f.doc("B", "mine.txt").write_text("B only")
    d = _docs(f.run("B"))
    assert f.doc("B", "x.txt").exists() and not f.hub_tree("mine.txt").exists()
    assert d["skipped_by_direction"] == 1 and d["stranded"] == ["mine.txt"]
    f.doc("B", "x.txt").unlink()
    d = _docs(f.run("B"))
    assert f.hub_tree("x.txt").exists() and d["deleted_hub_count"] == 0 and d["skipped_by_direction"] == 2
    assert f.run("B")["profiles"]["docs"]["status"] == "attention"


@pytestmark_rsync
def test_review_holds_on_hub(tmp_path, monkeypatch):
    f = Fleet(tmp_path, monkeypatch, conflict="review")
    f.doc("A", "c.txt").write_text("A1"); f.run("A"); f.run("B")
    f.doc("A", "c.txt").write_text("A2"); f.run("A")
    time.sleep(1.1)
    f.doc("B", "c.txt").write_text("B2")
    d = _docs(f.run("B"))
    assert d["conflicts"] == 1 and f.hub_tree("c.txt").read_text() == "A2" and f.doc("B", "c.txt").read_text() == "B2"
    holds = f.state.holds("docs")
    assert holds[0]["path"] == "Documents/c.txt" and holds[0]["incoming_origin"] == HOST
    assert f.state.status()["profiles"]["docs"]["holds"] == 1
    # unresolved: the next B run still sees the conflict (base kept the old entry)
    assert _docs(f.run("B"))["conflicts"] == 1
    assert f.state.release_hold("docs", "Documents/c.txt") is True and f.state.holds() == []


@pytestmark_rsync
def test_mass_delete_guard_live(tmp_path, monkeypatch):
    f = Fleet(tmp_path, monkeypatch)
    for i in range(600):
        f.doc("A", f"f{i}.txt").write_text(str(i))
    f.run("A"); f.run("B")
    for i in range(600):
        f.doc("A", f"f{i}.txt").unlink()
    rep = f.run("A")
    d = _docs(rep)
    assert d["mass_delete_held_count"] == 600 and d["deleted_hub_count"] == 0
    assert rep["profiles"]["docs"]["status"] == "attention" and f.hub_tree("f1.txt").exists()
    assert _docs(f.run("B"))["deleted_local_count"] == 0
    d = _docs(f.run("A", allow_mass_delete=True))
    assert d["deleted_hub_count"] == 600 and not f.hub_tree("f1.txt").exists()
    assert _docs(f.run("B"))["deleted_local_count"] == 600 and not f.doc("B", "f1.txt").exists()


@pytestmark_rsync
def test_full_mode_refuses_unindexed_hub_profile(tmp_path, monkeypatch):
    f = Fleet(tmp_path, monkeypatch)
    # simulate a pre-P7.2 hub: tree with content, indexed flag absent
    f.hub_tree("old.txt").parent.mkdir(parents=True, exist_ok=True)
    f.hub_tree("old.txt").write_text("old")
    f.state._conn.execute("DELETE FROM meta WHERE key LIKE 'indexed:%'")
    rep = f.run("A", expect_rc=1)
    assert rep["profiles"]["docs"]["status"] == "error"
    assert "not indexed" in rep["errors"][0]
    f.state.index_tree("docs", hs.local_index(f.hub_root / "docs" / "tree", recursive=True, exclude=[], workers=1))
    assert _docs(f.run("A"))["b_to_a"]["files_transferred"] == 1


@pytestmark_rsync
def test_dry_run_and_preview_full_mode(tmp_path, monkeypatch, capsys):
    f = Fleet(tmp_path, monkeypatch)
    f.doc("A", "a.txt").write_text("a"); f.run("A"); f.run("B")
    f.doc("A", "a.txt").unlink()
    f.doc("A", "n.txt").write_text("n")
    d = _docs(f.run("A", dry_run=True))
    assert d["deleted_hub"] == ["a.txt"] and d["a_to_b"]["files_planned"] == 1
    assert f.hub_tree("a.txt").exists() and not f.hub_tree("n.txt").exists()    # nothing happened
    f.be("A")
    assert hs._cmd_run(argparse.Namespace(all=True, profile=None, dry_run=False, workers=1, notify=False,
                                          plan_only=True, config=str(f.cfg), plan_progress=None)) == 0
    pv = json.loads(capsys.readouterr().out)
    path = pv["profiles"]["docs"]["paths"]["Documents"]
    assert path["mode"] == "full" and path["del_hub"] == 1 and path["a_to_b"] == 1 and path["b_to_a"] == 0


def test_daemon_state_journal_commit_routes(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from fsync.daemon import create_app
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "hub"
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"""
version: 2
hub: {{host: {HOST}, root: {root}}}
hosts: {{{HOST}: {{role: [hub, spoke]}}}}
shared: {{profiles: {{docs: {{paths: [Documents]}}}}}}
""")
    (root / "docs" / "tree" / "Documents").mkdir(parents=True)
    (root / "docs" / "tree" / "Documents" / "a.txt").write_text("A")
    st_ = (root / "docs" / "tree" / "Documents" / "a.txt").stat()
    c = TestClient(create_app(str(cfg), require_token=False))
    lease = c.post("/v1/hub/docs/lease", json={"host": "fury4dx", "run_id": "R1"}).json()["lease_id"]
    r = c.post("/v1/hub/docs/commit", json={"host": "fury4dx", "run_id": "R1", "lease_id": lease,
                                            "puts": [{"path": "Documents/a.txt", "size": st_.st_size,
                                                      "mtime": st_.st_mtime, "hash": "ha"}],
                                            "dels": [{"path": "Documents/old.txt"}], "holds": []})
    assert r.status_code == 200 and r.json()["accepted_puts"] == ["Documents/a.txt"], r.text
    seq = r.json()["seq_to"]
    s = c.get("/v1/hub/docs/state", params={"prefix": "Documents"}).json()
    assert s["paths"]["Documents/a.txt"][2] == "ha" and "Documents/old.txt" in s["tombstones"]
    j = c.get("/v1/hub/docs/journal", params={"since": 0}).json()
    assert [e[2] for e in j["entries"]] == ["put", "del"] and j["seq"] == seq
    assert c.post("/v1/hub/docs/commit", json={"host": "x", "run_id": "R2", "lease_id": "stale",
                                               "puts": [], "dels": [], "holds": []}).status_code == 410
    assert c.get("/v1/hub/holds").json() == []
    stt = c.get("/v1/hub/status").json()["profiles"]["docs"]
    assert stt["seq"] == seq and stt["live"] == 1 and stt["tombstones"] == 1
    assert stt["cursors"]["fury4dx"]["last_seq"] == seq
