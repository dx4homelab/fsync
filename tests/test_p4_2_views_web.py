"""Tests for P4.2 view builders and the web session/renderer."""

from dream4devops.ui_lite import Strip, Table, Actions, Tone
from dream4devops.ui_lite.render_web import to_html

from fsync import views


DAEMON = {"host": "boxa", "runner": None,
          "schedule": {"enabled": True, "interval_s": 3600, "next_ts": 9999999999},
          "last": {"run_id": "20260704-093901-x", "moved": 5, "conflicts": 2,
                   "errors": 0, "dry_run": False}}
PLAN = {"peer": "dev@peer", "profiles": {"documents": {"paths": {
    "Documents": {"a_to_b": 3, "b_to_a": 1, "bytes_a_to_b": 1000, "bytes_b_to_a": 20,
                  "overwrites_a_to_b": 1, "overwrites_b_to_a": 0, "conflicts": 0,
                  "identical": 800, "renames_demoted": 0, "samples": {"a_to_b": [], "b_to_a": []}}}}}}


def test_status_strip_variants():
    strip = views.status_strip(DAEMON, peer_line="peer boxb: idle")
    assert isinstance(strip, Strip)
    texts = [s.text for s in strip.segments]
    assert any("no sync running" in t for t in texts)
    assert any("5 moved, 2 held" in t for t in texts)
    assert "peer boxb: idle" in texts
    # daemon down
    down = views.status_strip(None)
    assert down.segments[0].tone == Tone.bad


def test_plan_screen_toggle_reflected():
    strip = views.status_strip(DAEMON)
    # both -> full push+pull counted
    both = views.plan_screen(PLAN, {"documents": "both"}, dry=False, strip=strip)
    html_both = to_html(both)
    assert "3 (1000B)" in html_both  # push count + bytes shown
    # off -> struck, excluded from totals
    off = views.plan_screen(PLAN, {"documents": "off"}, dry=False, strip=strip)
    html_off = to_html(off)
    assert "strike" in html_off
    assert "0/1 profiles active" in html_off
    # push -> pull column shows 'skipped'
    push = views.plan_screen(PLAN, {"documents": "push"}, dry=False, strip=strip)
    assert "1 skipped" in to_html(push)


def test_progress_screen_done_and_running():
    strip = views.status_strip(DAEMON)
    prog = {"status": "done", "started_ts": 100, "finished_ts": 130, "run_dir": "/x",
            "profiles": {"documents": {"paths": {"Documents": {
                "phase": "done", "a_to_b": {"files_transferred": 3, "overwrites_expected": 1},
                "b_to_a": {"files_transferred": 0}, "conflicts": 2, "seconds": 1.2}}}},
            "errors": []}
    scr = views.progress_screen(prog, "finished", strip)
    html = to_html(scr)
    assert "✓ done" in html and "2" in html and "held" in html


# --------------------------------------------------------------------------- #
# web session state machine (no real daemon: fake client)                     #
# --------------------------------------------------------------------------- #

class FakeClient:
    def __init__(self):
        self.started = []
        self.job_calls = 0
    def status(self):
        return DAEMON
    def plan_start(self, profiles=None):
        return "job1"
    def plan_get(self, job):
        self.job_calls += 1
        if self.job_calls < 2:
            return {"status": "running", "progress": {"profiles": {}}}
        return {"status": "done", "result": PLAN}
    def run_start(self, specs, dry_run=False):
        self.started.append((tuple(specs), dry_run))
        return {"run_id": "r-new", "running": True}
    def run_current(self):
        return {"running": False, "pointer": {"run_id": "r-new"},
                "progress": {"status": "done", "profiles": {}, "started_ts": 1, "finished_ts": 2}}
    def peer(self):
        return {"host": "boxb", "reachable": True, "busy": False}


def test_web_session_plan_toggle_run(monkeypatch):
    from fsync import web, certs
    monkeypatch.setattr(certs, "trusted_peers", lambda: [])  # web imports certs lazily
    s = web.WebSession(FakeClient(), profile_names=["documents"])
    for _ in range(5):  # poll1 starts job, poll2 running, poll3 done
        s.poll()
        if s.mode == "plan":
            break
    assert s.mode == "plan"
    assert s.state == {"documents": "both"}
    # cycle documents both->push
    s.act("1")
    assert s.state["documents"] == "push"
    assert s.run_specs() == ["documents=push"]
    # dry-run then run
    s.act("d")
    assert s.dry is True
    s.act("r")
    assert s.client.started == [(("documents=push",), True)]
    assert s.mode in ("progress", "finished")


def test_web_app_serves_shim_and_page():
    from fastapi.testclient import TestClient
    from fsync import web
    app = web.create_web_app(FakeClient(), profile_names=["documents"])
    c = TestClient(app)
    assert "hx-post" in c.get("/htmx.js").text
    assert c.get("/htmx.js").headers["content-type"].startswith("application/javascript")
    page = c.get("/").text
    assert page.startswith("<!doctype html>") and 'id="screen"' in page
    # no external resources anywhere in the served document
    assert "http://" not in page and "https://" not in page
