"""`fsync web` — the local browser UI, a thin renderer process (P4.2, R11).

A FastAPI app bound to 127.0.0.1 that holds one FsyncClient (so it carries the
loopback token; the browser never needs a credential) and renders the SAME
dream4ui-lite Screens the TUI uses — via ui_lite.render_web instead of
render_textual. Live updates use a tiny vanilla-JS shim (served locally, no
CDN) that implements the hx-post/hx-get subset the web renderer emits.

The browser talks only to this process; this process talks to fsyncd. Same
clean R9 boundary as the TUI — it imports fsync.views + fsync.client only.
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser

from fastapi import HTTPException, Request

from dream4devops.ui_lite import Action, Tone
from dream4devops.ui_lite.render_web import PAGE_CSS, page, to_html

from . import views
from .client import ApiError, DaemonUnavailable, FsyncClient

DEFAULT_WEB_PORT = 7445
POLL_MS = 1000

# minimal htmx-compatible shim: only what render_web emits — hx-post on
# buttons (click → POST → swap target) and hx-get + hx-trigger="every Nms"
# on #screen (poll → replace #screen). Keeps us CDN-free and dependency-free.
HTMX_SHIM = """
(function(){
  function swap(t,html,mode){ if(mode==='outerHTML'){t.outerHTML=html;}else{t.innerHTML=html;} }
  function wire(root){
    root.querySelectorAll('[hx-post]').forEach(function(el){
      if(el._w) return; el._w=1;
      el.addEventListener('click',function(e){
        e.preventDefault();
        fetch(el.getAttribute('hx-post'),{method:'POST'}).then(r=>r.text()).then(function(html){
          var sel=el.getAttribute('hx-target'); var t=sel?document.querySelector(sel):el;
          if(t){ swap(t,html,el.getAttribute('hx-swap')||'innerHTML'); rewire(); }
        }).catch(function(){});
      });
    });
  }
  var timer=null;
  function poll(){
    if(timer){clearInterval(timer);timer=null;}
    var el=document.querySelector('#screen[hx-get]'); if(!el) return;
    var m=(el.getAttribute('hx-trigger')||'').match(/every\\s+(\\d+)ms/); if(!m) return;
    var url=el.getAttribute('hx-get');
    timer=setInterval(function(){
      fetch(url).then(r=>r.text()).then(function(html){
        var cur=document.querySelector('#screen'); if(cur){cur.outerHTML=html; rewire();}
      }).catch(function(){});
    }, parseInt(m[1]));
  }
  function rewire(){ wire(document); poll(); }
  if(document.readyState!=='loading'){ rewire(); }
  else { document.addEventListener('DOMContentLoaded', rewire); }
})();
"""


class WebSession:
    """Per-process UI state, mirroring the TUI's state machine but driven by
    request polling instead of an async tick. Single local user; concurrent
    tabs intentionally share one session (same box, same person)."""

    CYCLE = {"both": "push", "push": "pull", "pull": "off", "off": "both"}

    def __init__(self, client: FsyncClient, profile_names: list[str] | None):
        self.client = client
        self.profile_names = profile_names
        self.lock = threading.RLock()   # poll() and act() both take it; reentrant
        self.mode = "planning"
        self.plan: dict | None = None
        self.plan_partial: dict | None = None
        self.plan_job: str | None = None
        self.state: dict[str, str] = {}
        self.dry = False
        self.run_id: str | None = None
        self.prev_run_id: str | None = None
        self.progress: dict | None = None
        self.msg = ""
        self.daemon_status: dict | None = None
        self.peer_line: str | None = None
        self._peer_next = 0.0

    def run_specs(self) -> list[str]:
        return [f"{n}={st}" for n in list((self.plan or {}).get("profiles", {}))
                if (st := self.state.get(n, "both")) != "off"]

    def _start_plan(self) -> None:
        self.mode = "planning"
        self.plan = self.plan_partial = None
        self.msg = ""
        try:
            self.plan_job = self.client.plan_start(self.profile_names)
        except (DaemonUnavailable, ApiError) as e:
            self.mode = "error"
            self.msg = f"plan failed: {e}"

    def poll(self) -> None:
        """Advance the state machine one step; called per fragment request.

        The peer probe (a blocking ssh/mTLS call that can take tens of seconds
        when box B is asleep) runs BEFORE taking the lock — it only sets
        peer_line, an atomic attribute write — so a slow peer never holds the
        lock. That matters for the GTK panel, where a button's act() waits on
        this same lock on the UI thread; holding it across network I/O froze
        the window."""
        now = time.time()
        if now >= self._peer_next:
            self._peer_next = now + 20
            self._refresh_peer()
        with self.lock:
            try:
                self.daemon_status = self.client.status()
                if self.mode == "no_daemon":
                    self._start_plan()
            except (DaemonUnavailable, ApiError):
                self.daemon_status = None
                self.mode = "no_daemon"
                self.msg = "fsyncd is not reachable"
                return

            if self.mode == "planning":
                if not self.plan_job:
                    self._start_plan()
                    return
                try:
                    job = self.client.plan_get(self.plan_job)
                except (DaemonUnavailable, ApiError):
                    return
                if job["status"] == "running":
                    self.plan_partial = job.get("progress")
                elif job["status"] == "done":
                    res = job["result"]
                    if not res.get("peer_reachable", True):
                        self.mode, self.msg = "no_peer", res.get("peer", "peer")
                    else:
                        self.plan = res
                        self.state = {n: p.get("direction", "both")
                                      for n, p in res.get("profiles", {}).items()}
                        self.mode = "plan"
                else:
                    self.mode, self.msg = "error", f"plan failed: {job.get('error')}"
            elif self.mode in ("progress", "spawning"):
                try:
                    cur = self.client.run_current()
                except (DaemonUnavailable, ApiError):
                    return
                if not cur:
                    return
                cur_id = cur["pointer"].get("run_id")
                # only adopt the run WE started (or, in spawning, a genuinely
                # new one) — never latch onto a scheduler run that raced us
                if self.mode == "spawning":
                    if self.run_id and cur_id != self.run_id:
                        return
                    if not self.run_id and cur_id == self.prev_run_id:
                        return
                elif cur_id != self.run_id:
                    return
                self.run_id = cur_id
                self.progress = cur["progress"]
                self.mode = ("progress" if cur.get("running")
                             else ("error" if cur["progress"].get("status") == "running" else "finished"))

    def _refresh_peer(self) -> None:
        # the LOCAL daemon does cross-box mTLS and relays it via /v1/peer;
        # this process just reads that relay (no direct peer connection).
        try:
            from . import views

            self.peer_line = views.peer_line(self.client.peer())
        except Exception:
            pass

    def act(self, key: str) -> None:
        with self.lock:
            if self.mode == "plan":
                if key == "r":
                    if not self.run_specs():
                        self.msg = "all profiles are off — cycle with 1-9"
                        return
                    self.prev_run_id, self.run_id, self.progress = self.run_id, None, None
                    self.mode = "spawning"
                    try:
                        resp = self.client.run_start(self.run_specs(), dry_run=self.dry)
                        self.run_id = resp.get("run_id")
                        self.mode = "progress"
                    except (DaemonUnavailable, ApiError) as e:
                        self.mode, self.msg = "error", f"run failed to start: {e}"
                elif key == "d":
                    self.dry = not self.dry
                elif key.isdigit():
                    names = list(self.plan.get("profiles", {}))
                    i = int(key) - 1
                    if 0 <= i < len(names):
                        self.state[names[i]] = self.CYCLE[self.state.get(names[i], "both")]
            if key == "p" and self.mode in ("plan", "no_peer", "finished", "error", "no_daemon"):
                self._start_plan()

    # ------------------------------------------------------------------ view

    def screen(self):
        # snapshot under the lock so mode/plan can't change mid-render (a
        # concurrent re-plan sets plan=None); build the view from the snapshot
        with self.lock:
            m, plan, state, dry, msg = self.mode, self.plan, dict(self.state), self.dry, self.msg
            partial, progress = self.plan_partial, self.progress
            strip = views.status_strip(self.daemon_status, self.peer_line)
        retry = [Action(key="p", label="retry", tone=Tone.accent)]
        if m == "no_daemon":
            content = views.message_screen(
                "fsyncd is not running",
                f"{msg}\n\nStart it: systemctl --user start fsync-daemon", Tone.bad, retry)
        elif m == "planning":
            content = (views.planning_screen(partial) if partial
                       else views.message_screen("", "Planning… backend is indexing both boxes", Tone.warn))
        elif m == "no_peer":
            content = views.message_screen("", f"Peer {msg} is not reachable — nothing to sync.",
                                           Tone.bad, retry)
        elif m == "plan" and plan is not None:
            content = views.plan_screen(plan, state, dry, msg)
        elif m == "spawning" or (m == "plan" and plan is None):
            content = views.message_screen("", "Starting…", Tone.warn)
        elif m == "error" and not progress:
            content = views.message_screen("", msg or "something went wrong", Tone.bad, retry)
        else:
            content = views.progress_screen(progress or {}, m)
        return views.with_strip(strip, content)

    def is_live(self) -> bool:
        """Whether the screen should keep polling (planning or a run in flight)."""
        return self.mode in ("planning", "spawning", "progress")


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _guard(request: Request) -> None:
    """Reject requests that a browser on another origin could have sent —
    closes CSRF (any site auto-POSTing /do/r → a real sync) and DNS-rebinding
    (a rebound attacker.com reading plan/paths). Two independent checks:

    - Host header must name loopback: after a DNS rebind the Host is still
      attacker.com, so this rejects the rebound read/drive.
    - Sec-Fetch-Site (sent by every modern browser, unforgeable by JS) must be
      same-origin or none (direct navigation). A cross-site fetch/form is
      'cross-site' → rejected. Non-browser callers (curl) omit it → allowed,
      but they still face the Host check.
    """
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in _LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="non-local Host refused")
    sfs = request.headers.get("sec-fetch-site")
    if sfs and sfs not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="cross-site request refused")


def create_web_app(client: FsyncClient, profile_names: list[str] | None = None):
    from fastapi import Depends, FastAPI
    from fastapi.responses import HTMLResponse, PlainTextResponse

    app = FastAPI(title="fsync web", dependencies=[Depends(_guard)])  # noqa: F811 (Depends)
    session = WebSession(client, profile_names)

    def _screen_div() -> str:
        inner = to_html(session.screen())
        poll = (f'hx-get="/frag" hx-trigger="every {POLL_MS}ms" hx-swap="outerHTML"'
                if session.is_live() else "")
        return f'<div id="screen" {poll}>{inner}</div>'

    @app.get("/htmx.js")
    def htmx() -> PlainTextResponse:
        return PlainTextResponse(HTMX_SHIM, media_type="application/javascript")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        session.poll()
        # full document; the #screen div already carries its poll attributes
        body = f"<style>{PAGE_CSS}</style><script src=\"/htmx.js\"></script>{_screen_div()}"
        return HTMLResponse(f'<!doctype html><html><head><meta charset="utf-8">'
                            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                            f'<title>fsync</title></head><body>{body}</body></html>')

    @app.get("/frag", response_class=HTMLResponse)
    def frag() -> HTMLResponse:
        session.poll()
        return HTMLResponse(_screen_div())

    @app.post("/do/{key}", response_class=HTMLResponse)
    def do(key: str) -> HTMLResponse:
        session.act(key)
        session.poll()
        return HTMLResponse(_screen_div())

    return app


def run_web(args) -> int:
    import uvicorn

    port = getattr(args, "web_port", None) or DEFAULT_WEB_PORT
    try:
        client = FsyncClient(port=getattr(args, "port", None) or 7444)
        client.status()  # fail fast if the daemon is down
    except DaemonUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    app = create_web_app(client, getattr(args, "profile", None) or None)
    url = f"http://127.0.0.1:{port}/"
    print(f"fsync web on {url}  (Ctrl-C to stop)")
    if not getattr(args, "no_browser", False):
        threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
