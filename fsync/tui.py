"""Textual TUI for fsync home-sync (P2 of docs/home-sync-automation.md).

Flow: plan preview ("what is coming") -> ONE confirmation -> the run executes
in a DETACHED process (start_new_session) that survives this UI -> live
progress polled from the runner's progress.json. Restarting the TUI while a
run is active re-attaches to it; after completion it shows the final state.

The TUI never holds the sync itself: killing it mid-run loses nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static

from .homesync import discover_run, state_root

POLL_SECONDS = 0.4

PHASE_LABEL = {
    "indexing": "indexing…",
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


class SyncTuiApp(App):
    TITLE = "fsync sync"
    CSS = """
    #body { padding: 1 2; }
    """
    BINDINGS = [
        Binding("q", "quit_ui", "Quit (run keeps going)"),
        Binding("r", "run_sync", "Execute"),
        Binding("d", "toggle_dry", "Toggle dry-run"),
        Binding("p", "replan", "Re-plan"),
    ]

    def __init__(self, config: str | None = None, profile_names: list[str] | None = None):
        super().__init__()
        self.config = config
        self.profile_names = profile_names
        self.mode = "loading"          # loading|planning|no_peer|plan|spawning|progress|finished|error
        self.plan: dict | None = None
        self.dry = False
        self.progress: dict | None = None
        self.run_dir: Path | None = None
        self.prev_run_id: str | None = None
        self.spawn_deadline = 0.0
        self.msg = ""

    # ------------------------------------------------------------------ setup

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(Static(id="body"))
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(POLL_SECONDS, self.tick)
        found = discover_run()
        if found:
            self.prev_run_id = found["pointer"].get("run_id")
            if found["running"]:
                self.attach(found)
                return
        self.start_plan()

    def attach(self, found: dict) -> None:
        self.run_dir = Path(found["pointer"]["run_dir"])
        self.progress = found["progress"]
        self.mode = "progress" if found["running"] else self._final_mode(found["progress"])
        self.render_body()

    @staticmethod
    def _final_mode(progress: dict) -> str:
        if progress.get("status") == "running":
            return "error"  # runner died mid-run (stale 'running' + dead pid)
        return "finished"

    # ------------------------------------------------------------------ plan

    def sel_args(self) -> list[str]:
        args: list[str] = []
        if self.profile_names:
            for n in self.profile_names:
                args += ["--profile", n]
        else:
            args.append("--all")
        if self.config:
            args += ["--config", self.config]
        return args

    def start_plan(self) -> None:
        self.mode = "planning"
        self.msg = ""
        self.render_body()
        self.run_worker(self._plan_worker, thread=True, exclusive=True)

    def _plan_worker(self) -> None:
        cmd = [sys.executable, "-m", "fsync.cli", "sync", "run", "--plan-only"] + self.sel_args()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        data = None
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
            except ValueError:
                pass
        self.call_from_thread(self._plan_ready, data, proc.stderr.strip())

    def _plan_ready(self, data: dict | None, stderr: str) -> None:
        if data is None:
            self.mode = "error"
            self.msg = f"plan failed: {stderr.splitlines()[-1] if stderr else 'no output'}"
        elif not data.get("peer_reachable", False):
            self.mode = "no_peer"
            self.msg = data.get("peer", "peer")
        else:
            self.plan = data
            self.mode = "plan"
        self.render_body()

    # ------------------------------------------------------------------ run

    def action_run_sync(self) -> None:
        if self.mode != "plan":
            return
        cmd = [sys.executable, "-m", "fsync.cli", "sync", "run"] + self.sel_args()
        if self.dry:
            cmd.append("--dry-run")
        log_path = state_root() / "tui-runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(Path.home()))
        self.mode = "spawning"
        self.spawn_deadline = time.time() + 30
        self.render_body()

    def action_toggle_dry(self) -> None:
        if self.mode == "plan":
            self.dry = not self.dry
            self.render_body()

    def action_replan(self) -> None:
        if self.mode in ("plan", "no_peer", "finished", "error"):
            self.start_plan()

    def action_quit_ui(self) -> None:
        self.exit(0)

    # ------------------------------------------------------------------ poll

    def tick(self) -> None:
        if self.mode == "spawning":
            found = discover_run()
            if found and found["pointer"].get("run_id") != self.prev_run_id:
                self.prev_run_id = found["pointer"].get("run_id")
                self.attach(found)
            elif time.time() > self.spawn_deadline:
                self.mode = "error"
                self.msg = f"runner did not start — see {state_root() / 'tui-runner.log'}"
                self.render_body()
        elif self.mode == "progress":
            found = discover_run()
            if found:
                self.progress = found["progress"]
                if not found["running"]:
                    self.mode = self._final_mode(found["progress"])
                self.render_body()

    # ------------------------------------------------------------------ render

    def render_body(self) -> None:
        body = self.query_one("#body", Static)
        if self.mode == "loading":
            body.update("starting…")
        elif self.mode == "planning":
            body.update(Text("Planning… indexing both boxes (cached hashing — usually seconds)", style="yellow"))
        elif self.mode == "no_peer":
            body.update(Text(f"Peer {self.msg} is not reachable — nothing to sync.\n\n"
                             "p: retry   q: quit", style="red"))
        elif self.mode == "plan":
            body.update(self._plan_table())
        elif self.mode == "spawning":
            body.update(Text("Starting detached sync run…", style="yellow"))
        elif self.mode in ("progress", "finished", "error"):
            body.update(self._progress_table())

    def _plan_table(self):
        assert self.plan is not None
        table = Table(title=f"Plan preview -> {self.plan.get('peer', '')}"
                            f"{'   [DRY RUN]' if self.dry else ''}",
                      expand=True)
        for col in ("profile / path", "→ push", "← pull", "overwrite→backup", "conflicts", "identical"):
            table.add_column(col, justify="right" if col != "profile / path" else "left")
        totals = [0, 0, 0, 0]
        total_bytes = 0
        for pname, pdata in self.plan.get("profiles", {}).items():
            for rel, d in pdata.get("paths", {}).items():
                over = d["overwrites_a_to_b"] + d["overwrites_b_to_a"]
                push = f"{d['a_to_b']} ({fmt_bytes(d['bytes_a_to_b'])})" if d["a_to_b"] else "-"
                pull = f"{d['b_to_a']} ({fmt_bytes(d['bytes_b_to_a'])})" if d["b_to_a"] else "-"
                style = "bold" if (d["a_to_b"] or d["b_to_a"] or d["conflicts"]) else "dim"
                table.add_row(f"{pname}/{rel}", push, pull,
                              str(over) if over else "-",
                              str(d["conflicts"]) if d["conflicts"] else "-",
                              str(d["identical"]), style=style)
                totals[0] += d["a_to_b"]; totals[1] += d["b_to_a"]
                totals[2] += over; totals[3] += d["conflicts"]
                total_bytes += d["bytes_a_to_b"] + d["bytes_b_to_a"]
        table.add_section()
        table.add_row("TOTAL", str(totals[0]), str(totals[1]), str(totals[2]),
                      str(totals[3]), "", style="bold cyan")
        hint = Text()
        hint.append(f"\n{fmt_bytes(total_bytes)} to move. Overwritten files are backed up on the "
                    f"receiver under ~/.fsync/backups/<run>/. Conflicts (review profiles) are held.\n\n")
        hint.append("r: execute", style="bold green")
        hint.append(f"   d: dry-run [{'ON' if self.dry else 'off'}]   p: re-plan   q: quit")
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
                lines.append(f"\n{conflicts_total} conflict(s) held for review — see "
                             f"{prog.get('run_dir', '')}/*/‌*.conflicts.json", style="yellow")
            lines.append(f"\nreport: {prog.get('run_dir', '')}/report.json", style="dim")
            lines.append("\np: plan a new run   q: quit", style="dim")
        from rich.console import Group
        return Group(table, lines)


def run_tui(args) -> int:
    profile_names = getattr(args, "profile", None) or None
    app = SyncTuiApp(config=getattr(args, "config", None), profile_names=profile_names)
    app.run()
    return 0
