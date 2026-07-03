"""Textual TUI for fsync home-sync (P2 of docs/home-sync-automation.md).

Flow: plan preview ("what is coming") -> ONE confirmation -> the run executes
in a DETACHED process (start_new_session) that survives this UI -> live
progress polled from the runner's progress.json. Restarting the TUI while a
run is active re-attaches to it; after completion it shows the final state.

The TUI never holds the sync itself: killing it mid-run loses nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static

from .homesync import discover_run, report_totals, state_root

POLL_SECONDS = 0.4
TIMER_PROBE_TICKS = 35  # systemd probe every ~14s; file reads happen every tick


def parse_systemd_show(text: str) -> dict:
    """Parse `systemctl show` KEY=VALUE output."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def timer_next_from_json(text: str) -> str | None:
    """Extract 'HH:MM' of the next elapse from `list-timers --output=json`.

    Works for monotonic timers too (whose NextElapseUSecRealtime is empty in
    `systemctl show`). Returns None when the timer is absent or unscheduled.
    """
    try:
        entries = json.loads(text)
        usec = entries[0].get("next")
        return time.strftime("%H:%M", time.localtime(usec / 1_000_000)) if usec else None
    except (ValueError, IndexError, KeyError, TypeError):
        return None

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
    #statusbar { padding: 0 2; height: 2; color: $text-muted; border-bottom: hkey $panel; }
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
        self.plan_partial: dict | None = None  # live per-path planning snapshot
        self.plan_progress_path: Path | None = None
        # per-profile run state, cycled by the profile's digit key:
        # both (⇅) -> push (→) -> pull (←) -> off (·) -> both …
        self.state: dict[str, str] = {}
        self.dry = False
        self.progress: dict | None = None
        self.run_dir: Path | None = None
        self.prev_run_id: str | None = None
        self.spawn_deadline = 0.0
        self.msg = ""
        # status strip state
        self.timer_state = "…"
        self.timer_next: str | None = None
        self._tick_n = 0
        self._activity_cache: tuple | None = None  # ((run_id, mtime), summary)

    # ------------------------------------------------------------------ setup

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="statusbar")
        yield VerticalScroll(Static(id="body"))
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(POLL_SECONDS, self.tick)
        found = discover_run()
        self.update_statusbar(found)
        self.run_worker(self._timer_probe, thread=True, exclusive=False)
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
        self.plan_partial = None
        self.plan_progress_path = state_root() / f"plan-progress-{os.getpid()}.json"
        self.plan_progress_path.unlink(missing_ok=True)
        self.render_body()
        self.run_worker(self._plan_worker, thread=True, exclusive=True)

    def _plan_worker(self) -> None:
        cmd = [sys.executable, "-m", "fsync.cli", "sync", "run", "--plan-only",
               "--plan-progress", str(self.plan_progress_path)] + self.sel_args()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        data = None
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
            except ValueError:
                pass
        self.call_from_thread(self._plan_ready, data, proc.stderr.strip())

    def _plan_ready(self, data: dict | None, stderr: str) -> None:
        if self.plan_progress_path:
            self.plan_progress_path.unlink(missing_ok=True)
        if data is None:
            self.mode = "error"
            self.msg = f"plan failed: {stderr.splitlines()[-1] if stderr else 'no output'}"
        elif not data.get("peer_reachable", False):
            self.mode = "no_peer"
            self.msg = data.get("peer", "peer")
        else:
            self.plan = data
            # start from each profile's configured direction
            self.state = {n: p.get("direction", "both")
                          for n, p in data.get("profiles", {}).items()}
            self.mode = "plan"
        self.render_body()

    def plan_profile_names(self) -> list[str]:
        return list((self.plan or {}).get("profiles", {}))

    CYCLE = {"both": "push", "push": "pull", "pull": "off", "off": "both"}
    STATE_MARK = {"both": ("⇅", "green"), "push": ("→", "cyan"),
                  "pull": ("←", "cyan"), "off": ("·", "dim")}

    def run_args(self) -> list[str]:
        """Runner selection args: every non-off profile is passed explicitly
        with its (possibly cycled) direction — the override is idempotent."""
        args: list[str] = []
        for n in self.plan_profile_names():
            st = self.state.get(n, "both")
            if st != "off":
                args += ["--profile", f"{n}={st}"]
        if self.config:
            args += ["--config", self.config]
        return args

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
        if all(st == "off" for st in self.state.values()):
            self.msg = "all profiles are off — cycle with 1-9"
            self.render_body()
            return
        cmd = [sys.executable, "-m", "fsync.cli", "sync", "run"] + self.run_args()
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
        self._tick_n += 1
        found = discover_run()
        self.update_statusbar(found)
        if self._tick_n % TIMER_PROBE_TICKS == 0:
            self.run_worker(self._timer_probe, thread=True, exclusive=False)

        if self.mode == "planning":
            try:
                self.plan_partial = json.loads(self.plan_progress_path.read_text())
                self.render_body()
            except (OSError, ValueError, AttributeError):
                pass  # snapshot not written yet (engine push / peer probe phase)
        elif self.mode == "spawning":
            if found and found["pointer"].get("run_id") != self.prev_run_id:
                self.prev_run_id = found["pointer"].get("run_id")
                self.attach(found)
            elif time.time() > self.spawn_deadline:
                self.mode = "error"
                self.msg = f"runner did not start — see {state_root() / 'tui-runner.log'}"
                self.render_body()
        elif self.mode == "progress":
            if found:
                self.progress = found["progress"]
                if not found["running"]:
                    self.mode = self._final_mode(found["progress"])
                self.render_body()

    # ------------------------------------------------------------------ status strip

    def _timer_probe(self) -> None:
        state = subprocess.run(
            ["systemctl", "--user", "is-active", "fsync-sync.timer"],
            capture_output=True, text=True,
        ).stdout.strip()
        timers = subprocess.run(
            ["systemctl", "--user", "list-timers", "fsync-sync.timer", "--output=json"],
            capture_output=True, text=True,
        ).stdout
        self.call_from_thread(self._timer_ready, state, timers)

    def _timer_ready(self, state: str, timers_json: str) -> None:
        self.timer_state = state or "unknown"
        self.timer_next = timer_next_from_json(timers_json)

    def _last_activity(self) -> str:
        try:
            run_id = (state_root() / "runs" / "latest").read_text().strip()
            rep_path = state_root() / "runs" / run_id / "report.json"
            key = (run_id, rep_path.stat().st_mtime)
        except OSError:
            return "no completed runs yet"
        if self._activity_cache and self._activity_cache[0] == key:
            return self._activity_cache[1]
        try:
            report = json.loads(rep_path.read_text())
        except (OSError, ValueError):
            return "no completed runs yet"
        moved, conflicts, errors = report_totals(report)
        when = f"{run_id[9:11]}:{run_id[11:13]}" if len(run_id) >= 13 else run_id
        summary = (f"last {when} — {moved} moved, {conflicts} held"
                   + (f", {errors} ERROR" if errors else "")
                   + (" [dry]" if report.get("dry_run") else ""))
        self._activity_cache = (key, summary)
        return summary

    def update_statusbar(self, found: dict | None) -> None:
        t = Text()
        if found and found["running"]:
            prog = found["progress"]
            elapsed = time.time() - (prog.get("started_ts") or time.time())
            t.append("● ", style="bold green")
            t.append(f"run {found['pointer']['run_id']} active "
                     f"(pid {found['pointer']['pid']}, {elapsed:.0f}s)", style="green")
        else:
            t.append("○ no sync running", style="dim")
        t.append("  ·  ", style="dim")
        if self.timer_state == "active":
            t.append(f"timer: next {self.timer_next}" if self.timer_next else "timer: on")
        elif self.timer_state in ("…", "unknown"):
            t.append("timer: …", style="dim")
        else:
            t.append("timer: off", style="yellow")
        t.append("  ·  ", style="dim")
        t.append(self._last_activity())
        self.query_one("#statusbar", Static).update(t)

    # ------------------------------------------------------------------ render

    def render_body(self) -> None:
        body = self.query_one("#body", Static)
        if self.mode == "loading":
            body.update("starting…")
        elif self.mode == "planning":
            if self.plan_partial:
                body.update(self._planning_table())
            else:
                body.update(Text("Planning… connecting to peer and pushing engine", style="yellow"))
        elif self.mode == "no_peer":
            body.update(Text(f"Peer {self.msg} is not reachable — nothing to sync.\n\n"
                             "p: retry   q: quit", style="red"))
        elif self.mode == "plan":
            body.update(self._plan_table())
        elif self.mode == "spawning":
            body.update(Text("Starting detached sync run…", style="yellow"))
        elif self.mode in ("progress", "finished", "error"):
            body.update(self._progress_table())

    def _planning_table(self):
        """Same shape as the plan preview, filling in as each path is planned."""
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
        active = sum(1 for s in self.state.values() if s != "off")
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
