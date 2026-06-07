"""Long-running fsync agent — the control-plane endpoint on each scanner host.

Loop: heartbeat -> poll ``scanner.command`` events addressed to this scanner ->
execute -> emit ack/result. Scans run as a subprocess (``fsync index``) in a
worker thread so the agent stays responsive (keeps heartbeating) and a scan
crash can't take the agent down.

Commands (event body ``command``):
  - ``ping``         -> ack + result {pong: true}
  - ``start_scan``   -> run a catalog index of ``args`` (dir, source, workers,
                        hash); emits scan.* progress via --events-url
  - ``stop``         -> shut the agent down after acking

Run:
    fsync agent --scanner-id z170 --events-url http://host:8080 \
                --catalog-url http://host:8081 --sources /mnt/m,/mnt/vhd
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .eventbus import EventConsumer, FsyncEvents, EV_COMMAND


class Agent:
    def __init__(
        self,
        scanner_id: str,
        events_url: str,
        *,
        catalog_url: Optional[str] = None,
        db_url: Optional[str] = None,
        sources: Optional[List[str]] = None,
        cursor_path: Optional[str] = None,
        poll_interval: float = 5.0,
        heartbeat_interval: float = 30.0,
        logger: Any | None = None,
    ):
        self.scanner_id = scanner_id
        self.events_url = events_url
        self.catalog_url = catalog_url
        self.db_url = db_url
        self.sources = sources or []
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.logger = logger

        self.events = FsyncEvents(events_url, scanner_id, logger=logger)
        # Fresh agents skip historical commands (start_ts=now) unless a cursor exists.
        self.consumer = EventConsumer(events_url, cursor_path=cursor_path, logger=logger, start_ts=int(time.time()))

        self.status = "idle"
        self._stop = False
        self._scan_thread: Optional[threading.Thread] = None
        self._current_run: Optional[str] = None

    # --- lifecycle --------------------------------------------------------
    def register(self) -> None:
        import platform

        self.events.registered(
            hostname=platform.node(),
            version="0.1.0",
            pid=os.getpid(),
            sources=self.sources,
            os=platform.platform(),
        )
        self._log("registered scanner %s (sources=%s)", self.scanner_id, self.sources)

    def run_forever(self) -> None:
        self.register()
        next_hb = 0.0
        try:
            while not self._stop:
                now = time.time()
                if now >= next_hb:
                    self.events.heartbeat(self.status, current_run_id=self._current_run)
                    next_hb = now + self.heartbeat_interval
                try:
                    for ev in self.consumer.poll(type=EV_COMMAND, instance=self.scanner_id):
                        self.handle_command(ev)
                except Exception as e:  # transient poll failure -> log and continue
                    self._log("poll error: %s", e)
                time.sleep(self.poll_interval)
        finally:
            self.events.heartbeat("offline")
            self.events.close()
            self.consumer.close()

    # --- command handling -------------------------------------------------
    def handle_command(self, ev: Dict[str, Any]) -> None:
        body = ev.get("body") or {}
        command_id = body.get("command_id") or ev.get("correlation_id") or ev["id"]
        cmd = body.get("command")
        self._log("command %s: %s args=%s", command_id, cmd, body.get("args"))
        self.events.command_ack(command_id, command=cmd)

        if cmd == "ping":
            self.events.command_result(command_id, "done", pong=True)
        elif cmd == "stop":
            self._stop = True
            self.events.command_result(command_id, "done", stopping=True)
        elif cmd == "start_scan":
            self._start_scan(body.get("args") or {}, command_id)
        else:
            self.events.command_result(command_id, "failed", error=f"unknown command: {cmd}")

    def _start_scan(self, args: Dict[str, Any], command_id: str) -> None:
        if self._scan_thread is not None and self._scan_thread.is_alive():
            self.events.command_result(command_id, "failed", error="busy: a scan is already running")
            return
        target = args.get("dir") or args.get("source")
        if not target:
            self.events.command_result(command_id, "failed", error="start_scan requires args.dir")
            return
        self._scan_thread = threading.Thread(target=self._run_scan, args=(args, command_id), daemon=True)
        self._scan_thread.start()

    def _run_scan(self, args: Dict[str, Any], command_id: str) -> None:
        from dream4devops.events import ulid_new

        run_id = args.get("run_id") or ulid_new()
        directory = args["dir"] if "dir" in args else args["source"]
        source = args.get("source") or directory
        self._current_run = run_id
        self.status = "scanning"
        cmd = [
            sys.executable, "-m", "fsync.cli", "index", str(directory),
            "--source", str(source),
            "--workers", str(args.get("workers", 4)),
            "--hash", str(args.get("hash", "sha256")),
            "--scanner-id", self.scanner_id,
            "--events-url", self.events_url,
            "--run-id", run_id,
        ]
        if self.catalog_url:
            cmd += ["--catalog-url", self.catalog_url]
        elif self.db_url:
            cmd += ["--db-url", self.db_url]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            ok = proc.returncode == 0
            self.events.command_result(
                command_id, "done" if ok else "failed",
                run_id=run_id, returncode=proc.returncode,
                stderr_tail=(proc.stderr or "")[-400:],
            )
            self._log("scan %s finished rc=%s", run_id, proc.returncode)
        except Exception as e:
            self.events.command_result(command_id, "failed", run_id=run_id, error=str(e)[:400])
        finally:
            self.status = "idle"
            self._current_run = None

    def _log(self, msg: str, *a: Any) -> None:
        if self.logger:
            self.logger.info(msg, *a)


def run_agent(args) -> int:
    sources = [s for s in (args.sources or "").split(",") if s.strip()]
    agent = Agent(
        scanner_id=args.scanner_id or os.environ.get("FSYNC_SCANNER_ID") or __import__("socket").gethostname(),
        events_url=args.events_url or os.environ.get("FSYNC_EVENTS_URL"),
        catalog_url=args.catalog_url or os.environ.get("FSYNC_CATALOG_URL"),
        db_url=args.db_url or os.environ.get("DB_URL"),
        sources=sources,
        cursor_path=args.cursor or None,
        poll_interval=args.poll_interval,
        heartbeat_interval=args.heartbeat_interval,
        logger=getattr(args, "logger", None),
    )
    if not agent.events_url:
        print("agent requires --events-url (or FSYNC_EVENTS_URL)", file=sys.stderr)
        return 2
    agent.run_forever()
    return 0
