"""0.11.0: `fsync risk` — drive-failure exposure report."""
import os
import subprocess
import time

from fsync import risk
from fsync.homesync import Profile

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _w(p, text="x", age_days=0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))


def _repo(path, ignore=None):
    path.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], env=ENV, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    _w(path / "tracked.txt", "tracked")
    if ignore:
        _w(path / ".gitignore", ignore)
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return run


def _world(tmp_path):
    home = tmp_path / "home"
    _w(home / "Documents/note.txt", "n" * 10)
    _w(home / "Documents/old.txt", "o" * 7, age_days=400)
    _w(home / "Documents/skip.tmp", "t" * 3)
    _w(home / ".bashrc", "b")
    _w(home / "Downloads/thesis.pdf", "p" * 100)
    _w(home / "Downloads/win.iso", "i" * 1000)
    _w(home / ".cache/thing/blob", "c" * 50)
    _w(home / ".config/app/settings.json", "s" * 20, age_days=90)
    _w(home / "ws/plain/node_modules/m/index.js", "j" * 30)
    _repo(home / "ws/owned", ignore="local.env\nbuild/\n")
    _w(home / "ws/owned/local.env", "SECRET=1")
    _w(home / "ws/owned/untracked.txt", "u")
    _repo(home / "ws/loner")                      # excluded from the git profile, no remote
    profiles = {
        "documents": Profile(name="documents", paths=["Documents"], exclude=["*.tmp"]),
        "dotfiles": Profile(name="dotfiles", paths=["."], recursive=False),
        "ws": Profile(name="ws", paths=["ws"], exclude=[".git", "node_modules"]),
    }
    state = tmp_path / "state"
    (state / "runs").mkdir(parents=True)
    (state / "runs" / "latest").write_text(time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 3600)) + "-abcd")
    _w(home / "Documents/before-last-sync.txt", "z", age_days=1)
    rep = risk.scan(home, profiles, git_owned={"ws/owned": "repos"}, svn_owned={},
                    extra_excludes={"ws": ["owned/*"]}, state_root=state)
    return home, rep


def test_scan_classifies_covered_replaceable_and_at_risk(tmp_path):
    _home, rep = _world(tmp_path)
    cov = rep["covered"]
    assert cov["profile documents"]["files"] == 3 and cov["profile dotfiles"]["files"] == 1
    # git snapshot: tracked + .gitignore + untracked + the .git dir, not the ignored file
    assert cov["git snapshots"]["files"] > 3
    rp = rep["replaceable"]
    assert rp["caches"]["files"] == 1 and rp["disk images (*.iso, *.qcow2, ...)"]["bytes"] == 1000
    assert rp["installed software (reinstall)"]["files"] == 1
    ar = rep["at_risk"]
    assert ar["Downloads"]["files"] == 1 and ar["Downloads"]["why"] == "in no sync profile"
    assert ar[".config/app"]["recent_files"] == 0
    assert ar["Documents"]["why"] == "excluded by '*.tmp' in profile documents"
    assert ar["ws/owned"]["samples"] == ["ws/owned/local.env"]
    assert "git-ignored" in ar["ws/owned"]["why"]
    # a repo no snapshot carries, with no remote: its history is at risk and it is listed
    assert "ws/loner/.git" in ar and "on no remote" in ar["ws/loner/.git"]["why"]
    assert [r["rel"] for r in rep["repos_without_snapshot"]] == ["ws/loner"]
    assert rep["repos_without_snapshot"][0]["remotes"] == 0
    # working files of that repo travel with the file profile
    assert cov["profile ws"]["files"] == 1
    t = rep["totals"]
    assert t["covered"]["files"] + t["replaceable"]["files"] + t["at_risk"]["files"] \
        == sum(len(f) for _, _, f in os.walk(_home))


def test_pending_counts_only_covered_files_newer_than_last_run(tmp_path):
    _home, rep = _world(tmp_path)
    # everything covered was written "now" except the two aged files
    cov_files = rep["totals"]["covered"]["files"]
    assert rep["pending"]["files"] == cov_files - 2
    assert rep["last_run"] is not None


def test_render_orders_recent_first_and_warns(tmp_path):
    _home, rep = _world(tmp_path)
    order = [k for k, _ in risk.at_risk_order(rep)]
    assert order.index("Downloads") < order.index(".config/app")       # recent before stale
    text = risk.render(rep, top=2, daemon_state="activating")
    assert "LOST, hard to replace" in text and "more group(s)" in text and "Largest of those" in text
    assert "sync service is 'activating'" in text
    assert "ws/loner: NO remote" in text
    rep["last_run"] = rep["generated"] - 2 * 86400
    assert "WARNING: the last sync run was 2 days ago" in risk.render(rep)


def test_replaceable_patterns():
    rc = risk.replaceable_category
    assert rc("node_modules/x/y.js") and rc("a/b/node_modules/x.js") and rc(".cache/x")
    assert rc("ws/proj/.venv/bin/python") and rc("x/disk.qcow2")
    assert rc("Documents/thesis.pdf") is None and rc(".ssh/id_ed25519") is None
    assert rc(".fsync/backups/r1/homelab/fsync/.git/x") and rc("workspaces/a/.git/x") is None
