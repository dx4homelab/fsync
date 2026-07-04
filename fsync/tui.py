"""Textual TUI for fsync home-sync — a THIN CLIENT of fsyncd (P4.1, R9/R11).

This module talks only to fsync.client (HTTP over TLS); it imports nothing
from the engine and reads no state files. Flow is unchanged from P2: plan
preview ("what is coming") -> ONE confirmation -> the run executes in the
backend (detached from this UI) -> live progress; restarting the TUI
re-attaches. The status strip shows this box's runner, the daemon schedule,
last-run stats — and, when a peer daemon is trusted, the peer's runner too.
"""

from __future__ import annotations

import time

from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static

from .client import ApiError, DaemonUnavailable, FsyncClient

POLL_SECONDS = 0.4
PEER_PROBE_TICKS = 75  # ssh + remote-API probes every ~30s

PHASE_LABEL = {
    "indexing": "indexing…",
    "merging": "⇄ merging",
    "a_to_b": "→ pushing",
    "b_to_a": "← pulling",
    "done": "done",
}


def fmt_bytes(n: int | None) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def fmt_clock(ts: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"


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
    STATE_MARK = {"both": ("⇅", "green"), "push": ("→", "cyan"),
                  "pull": ("←", "cyan"), "off": ("·", "dim")}

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

    # ------------------------------------------------------------------ poll

    def tick(self) -> None:
        self._tick_n += 1
        try:
            self.daemon_status = self.client.status()
        except (DaemonUnavailable, ApiError):
            self.daemon_status = None
            if self.mode not in ("no_daemon", "error"):
                self.mode = "no_daemon"
                self.msg = "fsyncd stopped responding"
                self.render_body()
        self.update_statusbar()
        if self._tick_n % PEER_PROBE_TICKS == 1:
            self.run_worker(self._peer_probe, thread=True, exclusive=False)

        if self.mode == "planning" and self.plan_job:
            try:
                job = self.client.plan_get(self.plan_job)
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
                current = self.client.run_current()
            except (DaemonUnavailable, ApiError):
                return
            if current and (self.run_id is None
                            or current["pointer"].get("run_id") == self.run_id
                            or self.mode == "spawning"):
                self.run_id = current["pointer"].get("run_id")
                self.progress = current["progress"]
                if not current.get("running"):
                    self.mode = self._final_mode(current["progress"])
                elif self.mode == "spawning":
                    self.mode = "progress"
                self.render_body()

    def _peer_probe(self) -> None:
        line = None
        try:
            from . import certs  # trust store lookup only — not engine code

            peers = certs.trusted_peers()
            if peers:
                st = FsyncClient.for_peer(peers[0], host=f"{peers[0]}.lan").status()
                runner = st.get("runner")
                line = (f"peer {st.get('host')}: ● run {runner['run_id']}"
                        if runner else f"peer {st.get('host')}: idle")
            else:
                info = self.client.peer()
                line = (f"peer {info['host']}: "
                        + ("busy" if info.get("busy")
                           else "reachable" if info.get("reachable") else "away"))
        except (DaemonUnavailable, ApiError, Exception):
            line = None
        self.call_from_thread(setattr, self, "peer_line", line)

    # ------------------------------------------------------------------ status strip

    def update_statusbar(self) -> None:
        t = Text()
        st = self.daemon_status
        if st is None:
            t.append("✗ fsyncd unreachable", style="bold red")
        else:
            runner = st.get("runner")
            if runner:
                t.append("● ", style="bold green")
                t.append(f"run {runner['run_id']} active "
                         f"(pid {runner['pid']}, {runner['elapsed']:.0f}s)", style="green")
            else:
                t.append("○ no sync running", style="dim")
            sched = st.get("schedule") or {}
            t.append("  ·  ", style="dim")
            if not sched.get("enabled"):
                t.append("schedule: off", style="yellow")
            else:
                t.append(f"next {fmt_clock(sched.get('next_ts'))}")
            last = st.get("last")
            t.append("  ·  ", style="dim")
            if last:
                t.append(f"last {last['run_id'][9:11]}:{last['run_id'][11:13]} — "
                         f"{last['moved']} moved, {last['conflicts']} held"
                         + (f", {last['errors']} ERROR" if last["errors"] else "")
                         + (" [dry]" if last.get("dry_run") else ""))
            else:
                t.append("no completed runs yet")
        if self.peer_line:
            t.append("  ·  ", style="dim")
            t.append(self.peer_line, style="cyan")
        self.query_one("#statusbar", Static).update(t)

    # ------------------------------------------------------------------ render

    def render_body(self) -> None:
        body = self.query_one("#body", Static)
        if self.mode == "loading":
            body.update("starting…")
        elif self.mode == "no_daemon":
            body.update(Text(f"fsyncd is not running.\n\n{self.msg}\n\n"
                             "Start it:  systemctl --user start fsync-daemon\n"
                             "Install:   fsync daemon install\n\n"
                             "p: retry   q: quit", style="red"))
        elif self.mode == "planning":
            if self.plan_partial:
                body.update(self._planning_table())
            else:
                body.update(Text("Planning… backend is indexing both boxes", style="yellow"))
        elif self.mode == "no_peer":
            body.update(Text(f"Peer {self.msg} is not reachable — nothing to sync.\n\n"
                             "p: retry   q: quit", style="red"))
        elif self.mode == "plan":
            body.update(self._plan_table())
        elif self.mode == "spawning":
            body.update(Text("Starting run in the backend…", style="yellow"))
        elif self.mode in ("progress", "finished", "error"):
            body.update(self._progress_table())

    def _planning_table(self):
        partial = self.plan_partial or {}
        done = total = 0
        table = Table(title="Planning… (both boxes hash locally; cached files are stat-only)",
                      expand=True)
        for col in ("profile / path", "state", "→ push", "← pull", "conflicts", "identical"):
            table.add_column(col, justify="right" if col not in ("profile / path", "state") else "left")
        for pname, pdata in partial.get("profiles", {}).items():
            for rel, d in pdata.get("paths", {}).items():
                total += 1
                phase = d.get("phase", "queued")
                if phase == "done":
                    done += 1
                    push = f"{d['a_to_b']} ({fmt_bytes(d['bytes_a_to_b'])})" if d.get("a_to_b") else "-"
                    pull = f"{d['b_to_a']} ({fmt_bytes(d['bytes_b_to_a'])})" if d.get("b_to_a") else "-"
                    style = "bold" if (d.get("a_to_b") or d.get("b_to_a") or d.get("conflicts")) else ""
                    table.add_row(f"{pname}/{rel}", Text("✓ planned", style="green"),
                                  push, pull,
                                  str(d.get("conflicts") or "-"), str(d.get("identical", "")),
                                  style=style)
                elif phase == "indexing":
                    table.add_row(f"{pname}/{rel}", Text("⣷ indexing…", style="yellow"),
                                  "", "", "", "")
                else:
                    table.add_row(f"{pname}/{rel}", Text("queued", style="dim"),
                                  "", "", "", "", style="dim")
        hint = Text(f"\n{done}/{total} paths planned — the preview with toggles appears when all "
                    "are in.   q: quit", style="dim")
        from rich.console import Group
        return Group(table, hint)

    def _plan_table(self):
        assert self.plan is not None
        table = Table(title=f"Plan preview -> {self.plan.get('peer', '')}"
                            f"{'   [DRY RUN]' if self.dry else ''}",
                      expand=True)
        for col in ("on", "profile / path", "→ push", "← pull", "overwrite→backup", "conflicts", "identical"):
            table.add_column(col, justify="right" if col not in ("on", "profile / path") else "left")
        totals = [0, 0, 0, 0]
        total_bytes = 0
        for i, (pname, pdata) in enumerate(self.plan.get("profiles", {}).items(), start=1):
            st = self.state.get(pname, "both")
            sym, sym_style = self.STATE_MARK[st]
            mark = Text(f"{i} ", style="bold cyan") + Text(sym, style=sym_style)
            take_push = st in ("both", "push")
            take_pull = st in ("both", "pull")
            for j, (rel, d) in enumerate(pdata.get("paths", {}).items()):
                over = (d["overwrites_a_to_b"] if take_push else 0) + \
                       (d["overwrites_b_to_a"] if take_pull else 0)
                push = (f"{d['a_to_b']} ({fmt_bytes(d['bytes_a_to_b'])})"
                        if d["a_to_b"] and take_push else
                        (Text(f"{d['a_to_b']} skipped", style="dim") if d["a_to_b"] else "-"))
                pull = (f"{d['b_to_a']} ({fmt_bytes(d['bytes_b_to_a'])})"
                        if d["b_to_a"] and take_pull else
                        (Text(f"{d['b_to_a']} skipped", style="dim") if d["b_to_a"] else "-"))
                if st == "off":
                    style = "dim strike"
                elif d["a_to_b"] or d["b_to_a"] or d["conflicts"]:
                    style = "bold"
                else:
                    style = "dim"
                table.add_row(mark if j == 0 else "", f"{pname}/{rel}", push, pull,
                              str(over) if over else "-",
                              str(d["conflicts"]) if d["conflicts"] else "-",
                              str(d["identical"]), style=style)
                if take_push:
                    totals[0] += d["a_to_b"]
                    total_bytes += d["bytes_a_to_b"]
                if take_pull:
                    totals[1] += d["b_to_a"]
                    total_bytes += d["bytes_b_to_a"]
                if st != "off":
                    totals[2] += over
                    totals[3] += d["conflicts"]
        active = len(self.run_specs())
        table.add_section()
        table.add_row("", f"TOTAL ({active}/{len(self.plan_profile_names())} profiles active)",
                      str(totals[0]), str(totals[1]), str(totals[2]),
                      str(totals[3]), "", style="bold cyan")
        hint = Text()
        hint.append(f"\n{fmt_bytes(total_bytes)} to move. Overwritten files are backed up on the "
                    f"receiver under ~/.fsync/backups/<run>/. Conflicts (review profiles) are held.\n")
        if self.msg:
            hint.append(f"{self.msg}\n", style="yellow")
        hint.append("\nr: execute", style="bold green")
        hint.append(f"   1-{len(self.plan_profile_names())}: cycle ⇅ both → push ← pull · off"
                    f"   d: dry-run [{'ON' if self.dry else 'off'}]   p: re-plan   q: quit")
        from rich.console import Group
        return Group(table, hint)

    def _progress_table(self):
        prog = self.progress or {}
        status = prog.get("status", "?")
        elapsed = (prog.get("finished_ts") or time.time()) - (prog.get("started_ts") or time.time())
        title = {
            "progress": f"Sync running (pid {prog.get('pid')}) — {elapsed:.0f}s",
            "finished": f"Sync {status.upper()} in {elapsed:.0f}s",
            "error": f"Sync ERROR after {elapsed:.0f}s",
        }[self.mode if self.mode in ("progress", "finished", "error") else "progress"]
        if prog.get("dry_run"):
            title += "   [DRY RUN]"
        table = Table(title=title, expand=True)
        for col in ("profile / path", "state", "→ push", "← pull", "conflicts", "time"):
            table.add_column(col, justify="right" if col not in ("profile / path", "state") else "left")

        conflicts_total = 0
        for pname, pdata in prog.get("profiles", {}).items():
            paths = pdata.get("paths", {})
            if not paths and pdata.get("status") == "pending":
                table.add_row(pname, Text("pending", style="dim"), "", "", "", "", style="dim")
                continue
            for rel, ps in paths.items():
                phase = ps.get("phase", "?")
                if phase == "done":
                    ab, ba = ps.get("a_to_b", {}), ps.get("b_to_a", {})
                    push = str(ab.get("files_transferred", 0)) + (
                        f" ({ab.get('overwrites_expected', 0)}⤺)" if ab.get("overwrites_expected") else "")
                    pull = str(ba.get("files_transferred", 0)) + (
                        f" ({ba.get('overwrites_expected', 0)}⤺)" if ba.get("overwrites_expected") else "")
                    cf = ps.get("conflicts", 0)
                    conflicts_total += cf
                    table.add_row(f"{pname}/{rel}", Text("✓ done", style="green"),
                                  push, pull, str(cf) if cf else "-",
                                  f"{ps.get('seconds', 0)}s")
                else:
                    label = PHASE_LABEL.get(phase, phase)
                    leg = ps.get("leg")
                    detail = ""
                    if leg:
                        detail = f"{fmt_bytes(leg.get('bytes'))}"
                        if leg.get("pct") is not None:
                            detail += f" {leg['pct']}%"
                        if leg.get("files_done") is not None and leg.get("files_total"):
                            detail += f" ({leg['files_done']}/{leg['files_total']} files)"
                    row_push = detail if (leg and leg.get("name") == "a_to_b") else ""
                    row_pull = detail if (leg and leg.get("name") == "b_to_a") else ""
                    table.add_row(f"{pname}/{rel}", Text(label, style="yellow"),
                                  row_push, row_pull, "", "")

        lines = Text()
        for err in prog.get("errors", []):
            lines.append(f"\nERROR: {err}", style="red")
        if self.mode == "progress":
            lines.append("\nRun continues even if you quit — `fsync tui` re-attaches.  q: quit", style="dim")
        else:
            if conflicts_total:
                lines.append(f"\n{conflicts_total} conflict(s) held for review", style="yellow")
            lines.append(f"\nreport: {prog.get('run_dir', '')}/report.json", style="dim")
            lines.append("\np: plan a new run   q: quit", style="dim")
        from rich.console import Group
        return Group(table, lines)


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
