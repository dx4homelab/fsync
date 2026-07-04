"""Textual TUI for fsync home-sync — a THIN CLIENT of fsyncd (P4.1, R9/R11).

This module talks only to fsync.client (HTTP over TLS); it imports nothing
from the engine and reads no state files. Flow is unchanged from P2: plan
preview ("what is coming") -> ONE confirmation -> the run executes in the
backend (detached from this UI) -> live progress; restarting the TUI
re-attaches. The status strip shows this box's runner, the daemon schedule,
last-run stats — and, when a peer daemon is trusted, the peer's runner too.
"""

from __future__ import annotations

import asyncio

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static

from dream4devops.ui_lite import Action, Screen, Tone
from dream4devops.ui_lite.render_textual import to_rich

from . import views
from .client import ApiError, DaemonUnavailable, FsyncClient

POLL_SECONDS = 0.4
PEER_PROBE_TICKS = 75  # ssh + remote-API probes every ~30s


class SyncTuiApp(App):
    TITLE = "fsync sync"
    CSS = """
    #statusbar { padding: 0 2; height: 2; color: $text-muted; border-bottom: hkey $panel; }
    #body { padding: 1 2; }
    """
    BINDINGS = [
        Binding("q", "quit_ui", "Quit (run keeps going)"),
        Binding("r", "run_sync", "Execute"),
        Binding("d", "toggle_dry", "Toggle dry-run"),
        Binding("p", "replan", "Re-plan"),
    ]

    def __init__(self, client: FsyncClient | None = None,
                 profile_names: list[str] | None = None):
        super().__init__()
        self._client = client
        self.profile_names = profile_names
        # loading|no_daemon|planning|no_peer|plan|spawning|progress|finished|error
        self.mode = "loading"
        self.plan: dict | None = None
        self.plan_partial: dict | None = None
        self.plan_job: str | None = None
        self.state: dict[str, str] = {}
        self.dry = False
        self.progress: dict | None = None
        self.run_id: str | None = None
        self.msg = ""
        # status strip
        self.daemon_status: dict | None = None
        self.peer_line: str | None = None
        self._tick_n = 0
        self._polling = False           # re-entrancy guard for the async tick
        self._prev_run_id: str | None = None  # last pointer we saw before spawning
        self._peer_client: FsyncClient | None = None

    @property
    def client(self) -> FsyncClient:
        if self._client is None:
            self._client = FsyncClient()
        return self._client

    # ------------------------------------------------------------------ setup

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="statusbar")
        yield VerticalScroll(Static(id="body"))
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(POLL_SECONDS, self.tick)
        try:
            current = self.client.run_current()
        except DaemonUnavailable as e:
            self.mode = "no_daemon"
            self.msg = str(e)
            self.render_body()
            return
        except ApiError:
            current = None
        if current and current.get("running"):
            self.attach(current)
            return
        self.start_plan()

    def attach(self, current: dict) -> None:
        self.progress = current["progress"]
        self.run_id = current["pointer"].get("run_id")
        self.mode = "progress" if current.get("running") else self._final_mode(current["progress"])
        self.render_body()

    @staticmethod
    def _final_mode(progress: dict) -> str:
        if progress.get("status") == "running":
            return "error"  # runner died mid-run (stale 'running' state)
        return "finished"

    # ------------------------------------------------------------------ plan

    def start_plan(self) -> None:
        self.mode = "planning"
        self.msg = ""
        self.plan_partial = None
        self.plan_job = None
        self.render_body()
        self.run_worker(self._plan_start_worker, thread=True, exclusive=True)

    def _plan_start_worker(self) -> None:
        try:
            job = self.client.plan_start(self.profile_names)
        except (DaemonUnavailable, ApiError) as e:
            self.call_from_thread(self._fail, f"plan failed: {e}")
            return
        self.call_from_thread(setattr, self, "plan_job", job)

    def _fail(self, message: str) -> None:
        self.mode = "error"
        self.msg = message
        self.render_body()

    def _plan_ready(self, result: dict) -> None:
        if not result.get("peer_reachable", True):
            self.mode = "no_peer"
            self.msg = result.get("peer", "peer")
        else:
            self.plan = result
            self.state = {n: p.get("direction", "both")
                          for n, p in result.get("profiles", {}).items()}
            self.mode = "plan"
        self.render_body()

    def plan_profile_names(self) -> list[str]:
        return list((self.plan or {}).get("profiles", {}))

    CYCLE = {"both": "push", "push": "pull", "pull": "off", "off": "both"}

    def run_specs(self) -> list[str]:
        return [f"{n}={st}" for n in self.plan_profile_names()
                if (st := self.state.get(n, "both")) != "off"]

    def on_key(self, event: events.Key) -> None:
        if self.mode != "plan" or not event.key.isdigit():
            return
        idx = int(event.key) - 1
        names = self.plan_profile_names()
        if 0 <= idx < len(names):
            name = names[idx]
            self.state[name] = self.CYCLE[self.state.get(name, "both")]
            self.render_body()

    # ------------------------------------------------------------------ run

    def action_run_sync(self) -> None:
        if self.mode != "plan":
            return
        if not self.run_specs():
            self.msg = "all profiles are off — cycle with 1-9"
            self.render_body()
            return
        # remember the current run pointer so 'spawning' waits for a NEW one
        self._prev_run_id = (self.daemon_status or {}).get("runner", {}) \
            .get("run_id") if (self.daemon_status or {}).get("runner") else None
        self.mode = "spawning"
        self.render_body()
        self.run_worker(self._run_start_worker, thread=True, exclusive=True)

    def _run_start_worker(self) -> None:
        try:
            resp = self.client.run_start(self.run_specs(), dry_run=self.dry)
        except (DaemonUnavailable, ApiError) as e:
            self.call_from_thread(self._fail, f"run failed to start: {e}")
            return
        self.call_from_thread(self._run_started, resp)

    def _run_started(self, resp: dict) -> None:
        self.run_id = resp.get("run_id")
        self.mode = "progress"
        self.render_body()

    def action_toggle_dry(self) -> None:
        if self.mode == "plan":
            self.dry = not self.dry
            self.render_body()

    def action_replan(self) -> None:
        if self.mode in ("plan", "no_peer", "finished", "error", "no_daemon"):
            self.start_plan()

    def action_quit_ui(self) -> None:
        self.exit(0)

    def on_unmount(self) -> None:
        if self._client is not None:
            self._client.close()
        if self._peer_client is not None:
            self._peer_client.close()

    # ------------------------------------------------------------------ poll

    async def tick(self) -> None:
        # All HTTP runs in a worker thread (asyncio.to_thread) so a slow or
        # stalled daemon never blocks the Textual event loop; a re-entrancy
        # guard stops ticks from stacking if a poll runs long.
        if self._polling:
            return
        self._polling = True
        try:
            self._tick_n += 1
            try:
                self.daemon_status = await asyncio.to_thread(self.client.status)
                if self.mode == "no_daemon":
                    # daemon came back — recover instead of staying stuck
                    self.start_plan()
            except (DaemonUnavailable, ApiError):
                self.daemon_status = None
                if self.mode not in ("no_daemon",):
                    self.mode = "no_daemon"
                    self.msg = "fsyncd stopped responding"
            self.update_statusbar()
            if self._tick_n % PEER_PROBE_TICKS == 1:
                self.run_worker(self._peer_probe, thread=True, exclusive=False)

            if self.mode == "planning" and self.plan_job:
                try:
                    job = await asyncio.to_thread(self.client.plan_get, self.plan_job)
                except (DaemonUnavailable, ApiError):
                    return
                if job["status"] == "running":
                    if job.get("progress"):
                        self.plan_partial = job["progress"]
                        self.render_body()
                elif job["status"] == "done":
                    self._plan_ready(job["result"])
                else:
                    self._fail(f"plan failed: {job.get('error')}")
            elif self.mode in ("progress", "spawning"):
                try:
                    current = await asyncio.to_thread(self.client.run_current)
                except (DaemonUnavailable, ApiError):
                    return
                if current is None:
                    return
                cur_id = current["pointer"].get("run_id")
                # in 'spawning', only adopt a run that is genuinely NEW (the
                # run_start reply set self.run_id; else require id != prev)
                if self.mode == "spawning":
                    if self.run_id and cur_id != self.run_id:
                        return
                    if not self.run_id and cur_id == self._prev_run_id:
                        return
                elif cur_id != self.run_id:
                    return
                self.run_id = cur_id
                self.progress = current["progress"]
                if not current.get("running"):
                    self.mode = self._final_mode(current["progress"])
                elif self.mode == "spawning":
                    self.mode = "progress"
                self.render_body()
        finally:
            self._polling = False

    def _peer_probe(self) -> None:
        from . import certs  # trust-store lookup only — not engine code

        line = None
        try:
            peers = certs.trusted_peers()
            if peers:
                if self._peer_client is None:
                    self._peer_client = FsyncClient.for_peer(peers[0], host=f"{peers[0]}.lan")
                st = self._peer_client.status()
                runner = st.get("runner")
                line = (f"peer {st.get('host')}: ● run {runner['run_id']}"
                        if runner else f"peer {st.get('host')}: idle")
            else:
                info = self.client.peer()
                line = (f"peer {info['host']}: "
                        + ("busy" if info.get("busy")
                           else "reachable" if info.get("reachable") else "away"))
        except Exception:
            # a transient peer/probe failure must not clear a good last line
            line = self.peer_line
        self.call_from_thread(setattr, self, "peer_line", line)

    # ------------------------------------------------------------------ status strip + render
    #
    # All screen content is built as dream4ui-lite Screen specs by fsync.views
    # (the single source of truth shared with `fsync web`) and rendered to Rich
    # here. The TUI owns state + polling + keybindings; it owns no layout.

    def _strip(self):
        return views.status_strip(self.daemon_status, self.peer_line)

    def update_statusbar(self) -> None:
        self.query_one("#statusbar", Static).update(to_rich(Screen(blocks=[self._strip()])))

    def render_body(self) -> None:
        body = self.query_one("#body", Static)
        strip = self._strip()
        if self.mode == "loading":
            screen = views.message_screen("", "starting…", Tone.muted, strip)
        elif self.mode == "no_daemon":
            screen = views.message_screen(
                "fsyncd is not running",
                f"{self.msg}\n\nStart it:  systemctl --user start fsync-daemon\n"
                "Install:   fsync daemon install", Tone.bad, strip,
                actions=[Action(key="p", label="retry", tone=Tone.accent),
                         Action(key="q", label="quit")])
        elif self.mode == "planning":
            screen = (views.planning_screen(self.plan_partial, strip) if self.plan_partial
                      else views.message_screen("", "Planning… backend is indexing both boxes",
                                                Tone.warn, strip))
        elif self.mode == "no_peer":
            screen = views.message_screen(
                "", f"Peer {self.msg} is not reachable — nothing to sync.", Tone.bad, strip,
                actions=[Action(key="p", label="retry", tone=Tone.accent),
                         Action(key="q", label="quit")])
        elif self.mode == "plan":
            screen = views.plan_screen(self.plan, self.state, self.dry, strip, self.msg)
        elif self.mode == "spawning":
            screen = views.message_screen("", "Starting run in the backend…", Tone.warn, strip)
        elif self.mode == "error" and not self.progress:
            screen = views.message_screen(
                "", self.msg or "something went wrong", Tone.bad, strip,
                actions=[Action(key="p", label="retry", tone=Tone.accent),
                         Action(key="q", label="quit")])
        else:  # progress | finished | error-with-progress
            screen = views.progress_screen(self.progress or {}, self.mode, strip)
        body.update(to_rich(screen))


def run_tui(args) -> int:
    try:
        client = FsyncClient(port=getattr(args, "port", None) or 7444)
    except DaemonUnavailable as e:
        print(f"error: {e}", file=__import__("sys").stderr)
        return 2
    app = SyncTuiApp(client=client,
                     profile_names=getattr(args, "profile", None) or None)
    app.run()
    return 0
