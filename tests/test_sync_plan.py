"""Tests for `fsync sync-plan`: the compare-report -> rsync projection."""

import json
import sys
from pathlib import Path
from subprocess import run, PIPE

import pytest

from fsync.fileindex import (
    build_sync_plan,
    render_rsync_command,
    render_delete_block,
    split_remote,
    _is_remote,
)


def _item(path, mtime=1.0, h="h"):
    return {"path": path, "name": path.split("/")[-1], "mtime": mtime, "hash": h}


def _report(paired_as_lists=False):
    """A representative compare report with one entry in every bucket."""
    conf = (_item("conf.txt", 5.0, "ahash"), _item("conf.txt", 9.0, "bhash"))
    rename = (_item("dir_a/x.bin", h="dup"), _item("dir_b/y.bin", h="dup"))
    if paired_as_lists:  # what JSON round-tripping produces
        conf = [conf[0], conf[1]]
        rename = [rename[0], rename[1]]
    return {
        "exact_matches": [(_item("same.txt"), _item("same.txt"))],
        "name_matches_diff_hash": [conf],
        "hash_matches_diff_name": [rename],
        "only_in_a": [_item("only_a.txt")],
        "only_in_b": [_item("only_b.txt")],
    }


# --------------------------------------------------------------------------- #
# build_sync_plan                                                             #
# --------------------------------------------------------------------------- #

def _paths(items):
    return [it["path"] for it in items]


def test_union_review_default():
    plan = build_sync_plan(_report())
    assert _paths(plan["a_to_b"]) == ["only_a.txt"]
    assert _paths(plan["b_to_a"]) == ["only_b.txt"]
    # the same-path/different-content file is NOT synced; it is a conflict
    assert len(plan["conflicts"]) == 1
    assert plan["conflicts"][0][0]["path"] == "conf.txt"
    assert len(plan["renames"]) == 1
    assert plan["noop"] == 1
    assert plan["a_delete"] == [] and plan["b_delete"] == []


def test_conflict_newer_routes_by_mtime():
    plan = build_sync_plan(_report(), conflict="newer")
    # B's conf.txt (mtime 9.0) is newer than A's (5.0) -> flows B -> A
    assert "conf.txt" in _paths(plan["b_to_a"])
    assert "conf.txt" not in _paths(plan["a_to_b"])
    assert plan["conflicts"] == []


def test_conflict_newer_tie_is_unresolved():
    rep = _report()
    rep["name_matches_diff_hash"] = [(_item("c", 7.0, "a"), _item("c", 7.0, "b"))]
    plan = build_sync_plan(rep, conflict="newer")
    assert len(plan["conflicts"]) == 1  # equal mtimes -> cannot decide


def test_conflict_a_wins_and_b_wins():
    a = build_sync_plan(_report(), conflict="a-wins")
    assert "conf.txt" in _paths(a["a_to_b"]) and a["conflicts"] == []
    b = build_sync_plan(_report(), conflict="b-wins")
    assert "conf.txt" in _paths(b["b_to_a"]) and b["conflicts"] == []


def test_mirror_a_to_b_proposes_deletes_and_resolves_conflicts():
    plan = build_sync_plan(_report(), mirror="a-to-b")
    assert set(_paths(plan["a_to_b"])) == {"only_a.txt", "conf.txt"}  # A wins
    assert _paths(plan["b_delete"]) == ["only_b.txt"]
    assert plan["b_to_a"] == [] and plan["conflicts"] == []


def test_mirror_b_to_a_is_symmetric():
    plan = build_sync_plan(_report(), mirror="b-to-a")
    assert set(_paths(plan["b_to_a"])) == {"only_b.txt", "conf.txt"}
    assert _paths(plan["a_delete"]) == ["only_a.txt"]
    assert plan["a_to_b"] == []


def test_tolerates_json_list_pairs():
    # A report loaded back from `compare --output` has 2-element lists, not tuples.
    plan = build_sync_plan(_report(paired_as_lists=True), conflict="newer")
    assert "conf.txt" in _paths(plan["b_to_a"])
    assert len(plan["renames"]) == 1


def test_invalid_policy_and_mirror_raise():
    with pytest.raises(ValueError):
        build_sync_plan(_report(), conflict="bogus")
    with pytest.raises(ValueError):
        build_sync_plan(_report(), mirror="sideways")


# --------------------------------------------------------------------------- #
# command rendering                                                           #
# --------------------------------------------------------------------------- #

def test_is_remote_and_split():
    assert _is_remote("developer@host:/home/x")
    assert _is_remote("host:/p")
    assert not _is_remote("/home/x")
    assert not _is_remote("./rel")
    assert not _is_remote("rel/path")
    assert split_remote("dev@h:/home/x") == ("dev@h", "/home/x")
    assert split_remote("/local/path") == (None, "/local/path")


def test_render_rsync_local_is_dry_by_default():
    cmd = render_rsync_command("plan.a_to_b.lst", "/srv/a", "/srv/b")
    assert cmd.startswith("rsync ")
    assert " -n " in f" {cmd} "          # dry-run by default
    assert "--files-from=plan.a_to_b.lst" in cmd
    assert "-e " not in cmd               # no ssh for two local paths
    assert cmd.endswith("/srv/a/ /srv/b/")


def test_render_rsync_remote_adds_ssh_and_live_drops_n():
    cmd = render_rsync_command(
        "p.lst", "/srv/a", "developer@10.55.0.2:/home/developer", dry_run=False
    )
    assert " -n " not in f" {cmd} "
    assert "-e 'ssh -T -c aes128-gcm@openssh.com -o Compression=no -x'" in cmd


def test_render_delete_block_local_and_remote():
    local = render_delete_block("d.lst", "/srv/b")
    assert 'rm -vf -- "$base/$f"' in local
    remote = render_delete_block("d.lst", "dev@h:/home/developer")
    assert remote.startswith("ssh ")
    assert "dev@h" in remote


def test_render_delete_block_quotes_dangerous_base():
    # A base with shell metacharacters must be quoted, never interpolated raw.
    block = render_delete_block("d.lst", "/srv/$(touch PWNED)")
    assert "base='/srv/$(touch PWNED)'" in block      # quoted assignment
    assert "$(touch PWNED)/$f" not in block            # no raw interpolation


# --------------------------------------------------------------------------- #
# CLI integration                                                            #
# --------------------------------------------------------------------------- #

def run_cli(args, cwd=None):
    cmd = [sys.executable, "-m", "fsync.cli"] + args
    return run(cmd, stdout=PIPE, stderr=PIPE, text=True, cwd=cwd)


def _make_dirs(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir(); b.mkdir()
    (a / "same.txt").write_text("S"); (b / "same.txt").write_text("S")     # exact
    (a / "conf.txt").write_text("Aconf"); (b / "conf.txt").write_text("Bconf")  # conflict
    (a / "only_a.txt").write_text("A")                                     # only_in_a
    (b / "only_b.txt").write_text("B")                                     # only_in_b
    # rename pair: identical content, different name; target sits in a subdir
    # that does NOT exist on B, to exercise the script's mkdir -p.
    (a / "sub").mkdir(); (a / "sub" / "ra.bin").write_text("DUP")
    (b / "rb.bin").write_text("DUP")
    return a, b


def test_sync_plan_generates_files(tmp_path):
    a, b = _make_dirs(tmp_path)
    out = tmp_path / "plan"
    res = run_cli(["sync-plan", str(a), str(b), "--out-dir", str(out)])
    assert res.returncode == 0, res.stderr
    assert (out / "run.sh").exists()
    assert (out / "plan.a_to_b.lst").read_text().split() == ["only_a.txt"]
    assert (out / "plan.b_to_a.lst").read_text().split() == ["only_b.txt"]
    assert (out / "plan.conflicts.txt").exists()
    assert "conf.txt" in (out / "plan.conflicts.txt").read_text()
    assert (out / "plan.renames.sh").exists()
    # default is a dry run
    assert " -n " in " " + (out / "run.sh").read_text() + " "


def test_sync_plan_execute_union_syncs_both_ways_but_not_conflicts(tmp_path):
    a, b = _make_dirs(tmp_path)
    out = tmp_path / "plan"
    res = run_cli([
        "sync-plan", str(a), str(b), "--out-dir", str(out),
        "--execute", "--no-ssh", "--rsync-flags", "-a -H -I",
    ])
    assert res.returncode == 0, res.stderr
    sh = run(["bash", str(out / "run.sh")], stdout=PIPE, stderr=PIPE, text=True)
    assert sh.returncode == 0, sh.stderr

    # unique files propagated both directions
    assert (b / "only_a.txt").read_text() == "A"
    assert (a / "only_b.txt").read_text() == "B"
    # the conflict was left untouched on both sides (NOT clobbered)
    assert (a / "conf.txt").read_text() == "Aconf"
    assert (b / "conf.txt").read_text() == "Bconf"


def test_sync_plan_mirror_deletes_are_commented_out(tmp_path):
    a, b = _make_dirs(tmp_path)
    out = tmp_path / "plan"
    res = run_cli([
        "sync-plan", str(a), str(b), "--out-dir", str(out), "--mirror", "a-to-b",
        "--execute", "--no-ssh", "--rsync-flags", "-a -H -I",
    ])
    assert res.returncode == 0, res.stderr
    assert (out / "plan.b_delete.lst").read_text().split() == ["only_b.txt"]
    run_sh = (out / "run.sh").read_text()
    # the delete step must be present but commented out (a safety seatbelt)
    assert "DELETE on B" in run_sh
    assert "\n# " in run_sh  # commented line exists

    sh = run(["bash", str(out / "run.sh")], stdout=PIPE, stderr=PIPE, text=True)
    assert sh.returncode == 0, sh.stderr
    # mirror a-to-b: A wins the conflict and only_a flows to B...
    assert (b / "only_a.txt").read_text() == "A"
    assert (b / "conf.txt").read_text() == "Aconf"
    # ...but only_b.txt is NOT deleted, because the delete block is commented out
    assert (b / "only_b.txt").exists()


def test_sync_plan_renames_script_runs(tmp_path):
    a, b = _make_dirs(tmp_path)
    out = tmp_path / "plan"
    # default --dest is the local resolved B, so renames.sh uses local mv.
    res = run_cli(["sync-plan", str(a), str(b), "--out-dir", str(out)])
    assert res.returncode == 0, res.stderr
    sh = run(["bash", str(out / "plan.renames.sh")], stdout=PIPE, stderr=PIPE, text=True)
    assert sh.returncode == 0, sh.stderr
    # B's rb.bin was renamed to A's path (sub/ra.bin), mkdir -p creating the dir
    assert (b / "sub" / "ra.bin").read_text() == "DUP"
    assert not (b / "rb.bin").exists()


def test_sync_plan_mirror_b_to_a_rename_targets_src_side(tmp_path):
    # Regression: mirror b-to-a must rename on A (src), turning A's name into
    # B's name — not touch the authoritative B side.
    a, b = _make_dirs(tmp_path)
    out = tmp_path / "plan"
    res = run_cli([
        "sync-plan", str(a), str(b), "--out-dir", str(out), "--mirror", "b-to-a",
    ])
    assert res.returncode == 0, res.stderr
    script = (out / "plan.renames.sh").read_text()
    # operates on A (src): rename A's sub/ra.bin -> B's name rb.bin
    assert str(a) in script and str(b) not in script
    assert "sub/ra.bin" in script and "rb.bin" in script
    # and the rename is wired as a live step in run.sh (not "optional")
    assert "required for mirror" in (out / "run.sh").read_text()


def test_sync_plan_mirror_b_to_a_rename_runs(tmp_path):
    a, b = _make_dirs(tmp_path)
    out = tmp_path / "plan"
    res = run_cli([
        "sync-plan", str(a), str(b), "--out-dir", str(out), "--mirror", "b-to-a",
    ])
    assert res.returncode == 0, res.stderr
    sh = run(["bash", str(out / "plan.renames.sh")], stdout=PIPE, stderr=PIPE, text=True)
    assert sh.returncode == 0, sh.stderr
    # A now holds the file under B's name; A's original name is gone
    assert (a / "rb.bin").read_text() == "DUP"
    assert not (a / "sub" / "ra.bin").exists()


def test_sync_plan_warns_on_skipped_paths(tmp_path):
    # A report whose items lack 'path' (e.g. compare run with --fields name,hash)
    # must warn loudly and not silently sync nothing.
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "exact_matches": [], "name_matches_diff_hash": [], "hash_matches_diff_name": [],
        "only_in_a": [{"name": "x", "hash": "h"}],  # no "path"
        "only_in_b": [],
    }))
    res = run_cli([
        "sync-plan", "--from-report", str(report),
        "--src", str(tmp_path / "A"), "--dest", str(tmp_path / "B"),
        "--out-dir", str(tmp_path / "plan"),
    ])
    assert res.returncode == 0
    assert "skipped" in res.stderr.lower()
    assert "SKIPPED 1" in res.stderr


def test_sync_plan_rejects_newline_endpoint(tmp_path):
    a, b = _make_dirs(tmp_path)
    res = run_cli([
        "sync-plan", str(a), str(b), "--src", "/home/dev\nrm -rf x",
        "--dest", str(b), "--out-dir", str(tmp_path / "plan"),
    ])
    assert res.returncode == 2
    assert "newline" in res.stderr


def test_sync_plan_bad_report_file(tmp_path):
    res = run_cli([
        "sync-plan", "--from-report", str(tmp_path / "missing.json"),
        "--src", "/a", "--dest", "/b", "--out-dir", str(tmp_path / "plan"),
    ])
    assert res.returncode == 2
    assert "cannot read" in res.stderr


def test_sync_plan_report_is_a_directory(tmp_path):
    # A directory (or unreadable file) must fail cleanly, not traceback.
    res = run_cli([
        "sync-plan", "--from-report", str(tmp_path),
        "--src", "/a", "--dest", "/b", "--out-dir", str(tmp_path / "plan"),
    ])
    assert res.returncode == 2
    assert "cannot read" in res.stderr
    assert "Traceback" not in res.stderr


def test_sync_plan_from_report(tmp_path):
    a, b = _make_dirs(tmp_path)
    report = tmp_path / "report.json"
    res = run_cli(["compare", str(a), str(b), "--output", str(report)])
    assert res.returncode == 0, res.stderr
    out = tmp_path / "plan"
    res = run_cli([
        "sync-plan", "--from-report", str(report),
        "--src", str(a), "--dest", str(b), "--out-dir", str(out),
    ])
    assert res.returncode == 0, res.stderr
    assert (out / "plan.a_to_b.lst").read_text().split() == ["only_a.txt"]


def test_sync_plan_from_report_requires_src_dest(tmp_path):
    a, b = _make_dirs(tmp_path)
    report = tmp_path / "report.json"
    run_cli(["compare", str(a), str(b), "--output", str(report)])
    res = run_cli(["sync-plan", "--from-report", str(report), "--out-dir", str(tmp_path / "p")])
    assert res.returncode == 2
    assert "requires --src and --dest" in res.stderr
