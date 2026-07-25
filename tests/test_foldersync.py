"""Tests for the VCS-aware ad-hoc folder sync (`fsync sync folder`).

The plan builder is pure, so most coverage needs no real svn/ssh — probing and
execution are exercised through injected fake runners.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fsync import foldersync as fx
from fsync.foldersync import (
    SvnState, Step, TIER_SAFE, TIER_CONFIRM, TIER_HOLD,
    build_svn_plan, probe_svn, detect_vcs, local_runner,
    execute_plan, _parse_status, _resolve_paths, unversioned_note,
)
from fsync.homesync import HomesyncError


def _cp(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess(args="x", returncode=rc, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------- #
# detection + status parsing                                                   #
# --------------------------------------------------------------------------- #

def test_detect_vcs_real_dirs(tmp_path):
    (tmp_path / "svnwc" / ".svn").mkdir(parents=True)
    (tmp_path / "gitwc" / ".git").mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    assert detect_vcs(local_runner, str(tmp_path / "svnwc")) == "svn"
    assert detect_vcs(local_runner, str(tmp_path / "gitwc")) == "git"
    assert detect_vcs(local_runner, str(tmp_path / "plain")) == "plain"
    assert detect_vcs(local_runner, str(tmp_path / "nope")) == "missing"


def test_parse_status_classifies_and_detects_lock():
    text = (
        "M       configs/final.yaml\n"
        "A       new/added.py\n"
        "D       old/gone.py\n"
        "C       merge/broken.py\n"
        "!       deployment/repos/yaml\n"
        "?       scratchpad/local.sh\n"
        "I       ignored.log\n"
    )
    b = _parse_status(text)
    assert b["modified"] == ["configs/final.yaml"]
    assert b["added"] == ["new/added.py"]
    assert b["deleted"] == ["old/gone.py"]
    assert b["conflicted"] == ["merge/broken.py"]
    assert b["missing"] == ["deployment/repos/yaml"]
    assert b["unversioned"] == ["scratchpad/local.sh"]
    assert b["_locked"] is False


def test_parse_status_lock_from_error():
    b = _parse_status("svn: E155004: Working copy '/x' locked; run 'svn cleanup'")
    assert b["_locked"] is True


def test_probe_svn_parses_sections():
    canned = (
        "@@FSYNC@@rev\n2330\n"
        "@@FSYNC@@url\nhttps://svn.example/trunk\n"
        "@@FSYNC@@server\n2684\n"
        "@@FSYNC@@status\nM       a.txt\n?       b.txt\n"
        "@@FSYNC@@end\n"
    )
    st = probe_svn(lambda cwd, cmd, timeout=None: _cp(canned), "/wc", "minis4dx")
    assert st.wc_rev == 2330
    assert st.server_rev == 2684 and st.server_reachable is True
    assert st.behind is True
    assert st.modified == ["a.txt"] and st.unversioned == ["b.txt"]
    assert st.has_local_mods is True


def test_probe_svn_server_unreachable():
    canned = ("@@FSYNC@@rev\n2678\n@@FSYNC@@url\nhttps://svn.example/trunk\n"
              "@@FSYNC@@server\nUNREACHABLE\n@@FSYNC@@status\n\n@@FSYNC@@end\n")
    st = probe_svn(lambda cwd, cmd, timeout=None: _cp(canned), "/wc", "fury4dx")
    assert st.wc_rev == 2678
    assert st.server_rev is None and st.server_reachable is False
    assert st.behind is False  # unknown server -> not "behind"


# --------------------------------------------------------------------------- #
# plan builder (pure)                                                          #
# --------------------------------------------------------------------------- #

def _plan(local: SvnState, peer: SvnState):
    return build_svn_plan(
        local, peer,
        local_path="/home/u/wc", peer_path="/home/u/wc",
        local_label=local.label, peer_label=peer.label,
        local_backup="/home/u/.fsync/backups/r/wc/local",
        peer_backup="/home/u/.fsync/backups/r/wc/peer",
        peer_target="u@peer",
    )


def test_plan_clean_behind_is_single_update():
    local = SvnState("minis", wc_rev=2330, server_rev=2684, server_reachable=True,
                     missing=["x"])
    peer = SvnState("fury", wc_rev=2684, server_rev=2684, server_reachable=True)
    steps = _plan(local, peer)
    assert len(steps) == 1
    s = steps[0]
    assert s.side == "local" and s.tier == TIER_SAFE
    assert s.commands == ["svn update"]
    assert "restores missing items" in s.rationale


def test_plan_uncommitted_backs_up_then_offers_commit():
    local = SvnState("minis", wc_rev=2684, server_rev=2684, server_reachable=True)
    peer = SvnState("fury", wc_rev=2678, server_rev=2684, server_reachable=True,
                    modified=["containers/final-code.yaml"])
    steps = _plan(local, peer)
    kinds = [(s.side, s.tier, s.title) for s in steps]
    # peer: backup (safe) -> update-merge (safe) -> commit (confirm)
    backup = [s for s in steps if s.backup]
    assert backup and backup[0].tier == TIER_SAFE
    assert backup[0].backup.endswith("uncommitted.patch")
    update = [s for s in steps if s.commands == ["svn update --accept postpone"]]
    assert update and update[0].tier == TIER_SAFE and update[0].backup is not None
    commit = [s for s in steps if s.tier == TIER_CONFIRM]
    assert len(commit) == 1 and "svn commit" in commit[0].commands[0]


def test_plan_conflict_holds_and_skips_update():
    local = SvnState("minis", wc_rev=2330, server_rev=2684, server_reachable=True,
                     conflicted=["merge/x.py"])
    peer = SvnState("fury", wc_rev=2684, server_rev=2684, server_reachable=True)
    steps = _plan(local, peer)
    assert len(steps) == 1
    assert steps[0].tier == TIER_HOLD
    # no update stacked on a conflicted WC
    assert all(s.commands != ["svn update"] for s in steps)


def test_plan_locked_offers_cleanup():
    local = SvnState("minis", wc_rev=2684, server_rev=2684, server_reachable=True,
                     locked=True)
    peer = SvnState("fury", wc_rev=2684, server_rev=2684, server_reachable=True)
    steps = _plan(local, peer)
    assert steps[0].tier == TIER_SAFE and steps[0].commands == ["svn cleanup"]


def test_plan_both_current_is_empty():
    local = SvnState("minis", wc_rev=2684, server_rev=2684, server_reachable=True)
    peer = SvnState("fury", wc_rev=2684, server_rev=2684, server_reachable=True)
    assert _plan(local, peer) == []


def test_plan_offline_transfer_is_manual_only():
    # fury has an uncommitted edit but can't reach the server; the WC->WC move
    # must be surfaced as MANUAL guidance, never an auto-run step.
    local = SvnState("minis", wc_rev=2684, server_rev=2684, server_reachable=True)
    peer = SvnState("fury", wc_rev=2678, server_rev=None, server_reachable=False,
                    modified=["a.yaml"])
    steps = _plan(local, peer)
    # peer backup step is still safe; the cross-box apply onto minis is HOLD.
    holds = [s for s in steps if s.tier == TIER_HOLD]
    assert holds and any("apply fury's uncommitted change" in s.title for s in holds)
    assert all(not (s.tier == TIER_SAFE and "svn patch" in " ".join(s.commands)) for s in steps)


def test_non_destructive_invariant_holds_for_all_safe_steps():
    """Every Tier<=1 step must be additive (svn update/cleanup on a clean WC) or
    write a backup first — never a bare commit/revert/patch-apply."""
    local = SvnState("minis", wc_rev=2330, server_rev=2684, server_reachable=True,
                     modified=["m.py"], missing=["x"])
    peer = SvnState("fury", wc_rev=2680, server_rev=2684, server_reachable=True)
    for s in _plan(local, peer):
        if s.tier > TIER_SAFE:
            continue
        joined = " ".join(s.commands)
        assert "svn commit" not in joined
        assert "svn revert" not in joined
        assert "--remove-unversioned" not in joined
        mutating_update = "svn update --accept postpone" in joined
        if mutating_update:
            assert s.backup, f"merge-update without backup: {s.title}"


# --------------------------------------------------------------------------- #
# executor honors the tiers                                                    #
# --------------------------------------------------------------------------- #

class _Rec:
    def __init__(self, rc=0, stdout=""):
        self.calls = []
        self.rc, self.stdout = rc, stdout

    def __call__(self, cwd, cmd, timeout=None):
        self.calls.append(cmd)
        return _cp(self.stdout, self.rc)


def _step(tier, commands, needs=None):
    return Step(tier, "local", "/wc", "t", commands, "why", needs=needs)


def test_safe_step_runs_on_yes_skips_on_no():
    rec = _Rec()
    execute_plan([_step(TIER_SAFE, ["svn update"])], {"local": rec},
                 assume_yes=False, ask=lambda p: "y", log=lambda *a: None)
    assert rec.calls == ["svn update"]

    rec2 = _Rec()
    execute_plan([_step(TIER_SAFE, ["svn update"])], {"local": rec2},
                 assume_yes=False, ask=lambda p: "n", log=lambda *a: None)
    assert rec2.calls == []


def test_assume_yes_runs_safe_without_prompt():
    rec = _Rec()

    def boom(p):
        raise AssertionError("should not prompt under --yes for a safe step")

    execute_plan([_step(TIER_SAFE, ["svn cleanup"])], {"local": rec},
                 assume_yes=True, ask=boom, log=lambda *a: None)
    assert rec.calls == ["svn cleanup"]


def test_confirm_step_needs_token_even_with_assume_yes():
    # --yes must NOT auto-run a server-writing commit.
    rec = _Rec()
    asked = []

    def ask(p):
        asked.append(p)
        return ""  # decline

    execute_plan([_step(TIER_CONFIRM, ["svn commit -m x"])], {"local": rec},
                 assume_yes=True, ask=ask, log=lambda *a: None)
    assert asked, "confirm step must prompt even under --yes"
    assert rec.calls == []  # declined -> nothing ran

    rec2 = _Rec()
    execute_plan([_step(TIER_CONFIRM, ["svn commit -m x"])], {"local": rec2},
                 assume_yes=True, ask=lambda p: "COMMIT", log=lambda *a: None)
    assert rec2.calls == ["svn commit -m x"]


def test_hold_step_never_runs():
    rec = _Rec()
    execute_plan([_step(TIER_HOLD, ["svn resolve"])], {"local": rec},
                 assume_yes=True, ask=lambda p: "y", log=lambda *a: None)
    assert rec.calls == []


def test_comment_only_commands_are_skipped():
    rec = _Rec()
    execute_plan([_step(TIER_SAFE, ["# just a note", "svn update"])], {"local": rec},
                 assume_yes=True, ask=lambda p: "y", log=lambda *a: None)
    assert rec.calls == ["svn update"]  # comment not executed


# --------------------------------------------------------------------------- #
# path resolution + notes                                                      #
# --------------------------------------------------------------------------- #

def test_resolve_paths_under_home(tmp_path, monkeypatch):
    home = tmp_path / "home" / "u"
    (home / "workspaces" / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    local_abs, peer_abs, rel = _resolve_paths(str(home / "workspaces" / "proj"), "/peer/home")
    assert rel == "workspaces/proj"
    assert peer_abs == "/peer/home/workspaces/proj"


def test_resolve_paths_outside_home_rejected(tmp_path, monkeypatch):
    home = tmp_path / "home" / "u"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(HomesyncError):
        _resolve_paths("/etc", "/peer/home")


def test_unversioned_note_counts():
    local = SvnState("minis", unversioned=["a", "b"])
    peer = SvnState("fury", unversioned=["c"])
    note = unversioned_note(local, peer)
    assert "2 on minis" in note and "1 on fury" in note
    assert unversioned_note(SvnState("m"), SvnState("f")) is None
