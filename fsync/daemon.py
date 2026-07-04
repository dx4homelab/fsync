"""fsyncd — the operational backend for home-sync (P4.1, R9/R10).

Architecture: an API over the engine's existing on-disk state. Runs execute
in DETACHED subprocesses (the same `fsync sync run` the CLI and timer used),
so a daemon restart never kills a transfer, headless/CLI runs remain visible
through the API, and there is exactly one execution code path. The daemon
adds: REST access to state, plan jobs, an internal scheduler (absorbing the
systemd timer), and TLS/mTLS listeners.

Listeners (both port `daemon.port`, default 7444):
  - 127.0.0.1  — TLS, no client cert (local UIs; same-user trust)
  - LAN addr   — TLS with CERT_REQUIRED against pinned peer certs (mTLS);
                 only started when at least one peer is trusted

UI clients live in fsync.client / fsync.tui and import nothing from here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from . import certs
from .homesync import (
    DIRECTIONS,
    HomesyncError,
    discover_run,
    load_config,
    peer_busy,
    peer_reachable,
    report_totals,
    state_root,
)

API_VERSION = "1"
DEFAULT_PORT = 7444
MAX_PLAN_JOBS_INFLIGHT = 4
RUNS_KEEP = 200       # retain this many run dirs; GC the rest after each run
CONFLICTS_SCAN_CAP = 400  # bound the /v1/conflicts glob
SPAN_RE = re.compile(r"^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?\s*(?:(\d+)\s*s)?\s*$")


def parse_span(text: str, default_s: int = 3600) -> int:
    """'1h' / '30min' / '90s' / '1h30min' -> seconds."""
    m = SPAN_RE.match(str(text))
    if not m or not any(m.groups()):
        return default_s
    h, mn, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mn * 60 + s or default_s


def load_daemon_cfg(config_path: str | None) -> dict[str, Any]:
    import yaml

    cfg_path = Path(config_path or "~/.config/fsync/sync-profiles.yaml").expanduser()
    raw: dict[str, Any] = {}
    try:
        raw = (yaml.safe_load(cfg_path.read_text()) or {}).get("daemon") or {}
    except OSError:
        pass
    return {
        "port": int(raw.get("port", DEFAULT_PORT)),
        "interval_s": parse_span(raw.get("interval", "1h")),
        "jitter_s": parse_span(raw.get("jitter", "240s"), 240),
        "lan_host": raw.get("lan_host", "auto"),
    }


# --------------------------------------------------------------------------- #
# schedule state (survives daemon restarts; PUT /v1/schedule edits this, not  #
# the human-owned yaml)                                                       #
# --------------------------------------------------------------------------- #

def _schedule_path() -> Path:
    return state_root() / "schedule.json"


def read_schedule(defaults: dict) -> dict:
    try:
        data = json.loads(_schedule_path().read_text())
    except (OSError, ValueError):
        data = {}
    return {
        "enabled": bool(data.get("enabled", True)),
        "interval_s": int(data.get("interval_s", defaults["interval_s"])),
    }


def write_schedule(sched: dict) -> None:
    _schedule_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = _schedule_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(sched))
    os.replace(tmp, _schedule_path())


# --------------------------------------------------------------------------- #
# engine-facing helpers (spawn detached runners; read state files)            #
# --------------------------------------------------------------------------- #

def spawn_run(selection: list[str], dry_run: bool = False, notify: bool = False,
              config: str | None = None) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "fsync.cli", "sync", "run", *selection]
    if dry_run:
        cmd.append("--dry-run")
    if notify:
        cmd.append("--notify")
    if config:
        cmd += ["--config", config]
    log_path = state_root() / "daemon-runner.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    try:
        return subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=str(Path.home()))
    finally:
        log.close()  # the child dups the fd; our handle can close


def gc_run_dirs(keep: int = RUNS_KEEP) -> int:
    """Remove all but the newest `keep` run dirs; returns count deleted.

    The scheduler creates a run dir per fire; without this the state tree grows
    without bound. Never touches the run named by runs/latest."""
    import shutil

    runs = state_root() / "runs"
    if not runs.is_dir():
        return 0
    try:
        latest = (runs / "latest").read_text().strip()
    except OSError:
        latest = None
    dirs = sorted((p for p in runs.iterdir() if p.is_dir()), reverse=True)
    deleted = 0
    for d in dirs[keep:]:
        if d.name == latest:
            continue
        shutil.rmtree(d, ignore_errors=True)
        deleted += 1
    return deleted


def last_run_summary() -> dict | None:
    try:
        run_id = (state_root() / "runs" / "latest").read_text().strip()
        report = json.loads((state_root() / "runs" / run_id / "report.json").read_text())
    except (OSError, ValueError):
        return None
    moved, conflicts, errors = report_totals(report)
    finished_ts = None
    try:
        prog = json.loads((state_root() / "runs" / run_id / "progress.json").read_text())
        finished_ts = prog.get("finished_ts")
    except (OSError, ValueError):
        pass
    return {"run_id": run_id, "moved": moved, "conflicts": conflicts,
            "errors": errors, "dry_run": bool(report.get("dry_run")),
            "finished_ts": finished_ts}


def run_dir_for(run_id: str) -> Path:
    # run ids are generated by the engine; refuse anything path-like
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise HTTPException(status_code=400, detail="bad run id")
    d = state_root() / "runs" / run_id
    if not d.is_dir():
        raise HTTPException(status_code=404, detail="unknown run")
    return d


# --------------------------------------------------------------------------- #
# plan jobs                                                                   #
# --------------------------------------------------------------------------- #

class PlanJobs:
    def __init__(self, config: str | None):
        self.config = config
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def inflight(self) -> int:
        with self.lock:
            return sum(1 for j in self.jobs.values()
                       if j["result"] is None and j["error"] is None)

    def start(self, profiles: list[str] | None, workers: int | None) -> str:
        # each plan forks an ssh+hashing subprocess; cap concurrency so a UI
        # (or a stuck caller) mashing plan can't fork-bomb the box.
        if self.inflight() >= MAX_PLAN_JOBS_INFLIGHT:
            raise HTTPException(status_code=429,
                                detail=f"{MAX_PLAN_JOBS_INFLIGHT} plan jobs already running")
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        progress_path = state_root() / "plan-jobs" / f"{job_id}.json"
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "fsync.cli", "sync", "run", "--plan-only",
               "--plan-progress", str(progress_path)]
        for p in profiles or []:
            cmd += ["--profile", p]
        if not profiles:
            cmd.append("--all")
        if workers:
            cmd += ["--workers", str(workers)]
        if self.config:
            cmd += ["--config", self.config]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True)
        with self.lock:
            self.jobs[job_id] = {"proc": proc, "progress_path": progress_path,
                                 "result": None, "error": None, "started": time.time()}
            self._prune()
        threading.Thread(target=self._collect, args=(job_id, proc), daemon=True).start()
        return job_id

    def _collect(self, job_id: str, proc: subprocess.Popen) -> None:
        out, err = proc.communicate()
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            if proc.returncode == 0:
                try:
                    job["result"] = json.loads(out)
                except ValueError:
                    job["error"] = "plan produced no JSON"
            else:
                job["error"] = (err.strip().splitlines() or ["plan failed"])[-1]

    def get(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown plan job")
        progress = None
        try:
            progress = json.loads(Path(job["progress_path"]).read_text())
        except (OSError, ValueError):
            pass
        status = ("error" if job["error"]
                  else "done" if job["result"] is not None
                  else "running")
        return {"job_id": job_id, "status": status, "progress": progress,
                "result": job["result"], "error": job["error"]}

    def _prune(self, keep: int = 20) -> None:
        for jid in sorted(self.jobs)[:-keep]:
            job = self.jobs[jid]
            if job["result"] is not None or job["error"] is not None:
                Path(job["progress_path"]).unlink(missing_ok=True)
                del self.jobs[jid]


# --------------------------------------------------------------------------- #
# scheduler (absorbs the systemd timer: interval + jitter after each run)     #
# --------------------------------------------------------------------------- #

class Scheduler:
    def __init__(self, daemon_cfg: dict, config: str | None,
                 run_lock: threading.Lock):
        self.cfg = daemon_cfg
        self.config = config
        self.run_lock = run_lock  # shared with POST /v1/runs so they can't race
        self.next_ts: float | None = None

    def _base_after(self, sched: dict) -> float:
        last = last_run_summary()
        base = (last or {}).get("finished_ts") or time.time()
        return base + sched["interval_s"] + random.uniform(0, self.cfg["jitter_s"])

    async def run(self) -> None:
        sched = read_schedule(self.cfg)
        self.next_ts = max(self._base_after(sched), time.time() + 60)
        while True:
            await asyncio.sleep(15)
            sched = read_schedule(self.cfg)
            if not sched["enabled"]:
                self.next_ts = None
                continue
            if self.next_ts is None:
                self.next_ts = time.time() + sched["interval_s"]
            if time.time() < self.next_ts:
                continue
            found = discover_run()
            if found and found["running"]:
                # A run is active (ours or a manual/CLI one). Re-anchor to
                # FINISH: don't re-fire until interval after it ends, so a run
                # longer than the interval never immediately re-triggers.
                self.next_ts = time.time() + 300
                continue
            # take the shared lock so a concurrent POST /v1/runs can't also spawn
            if not self.run_lock.acquire(blocking=False):
                self.next_ts = time.time() + 30
                continue
            try:
                spawn_run(["--all"], notify=True, config=self.config)
                _await_pointer(None, timeout=15)  # let the runner register
                gc_run_dirs()
            finally:
                self.run_lock.release()
            sched = read_schedule(self.cfg)
            self.next_ts = time.time() + sched["interval_s"] + random.uniform(0, self.cfg["jitter_s"])


def _await_pointer(prev_run_id: str | None, timeout: float) -> dict | None:
    """Block until a NEW run pointer appears (the runner registered) or the
    timeout elapses. Returns the discover_run() dict, or None on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.25)
        found = discover_run()
        if found and found["pointer"].get("run_id") != prev_run_id:
            return found
    return None


class RunBody(BaseModel):
    profiles: list[str] | None = None
    dry_run: bool = False


class PlanBody(BaseModel):
    profiles: list[str] | None = None
    workers: int | None = None


class ScheduleBody(BaseModel):
    enabled: bool | None = None
    interval: str | None = None
    interval_s: int | None = None


# --------------------------------------------------------------------------- #
# app                                                                         #
# --------------------------------------------------------------------------- #

def create_app(config: str | None = None, *, require_token: bool = False,
               shared: dict | None = None) -> FastAPI:
    """Build the API app.

    ``require_token`` gates every endpoint behind the loopback bearer token
    (the 127.0.0.1 listener uses this — TLS there authenticates the server,
    not the caller, and every local user can reach loopback). The mTLS
    listener sets it False: peers are already authenticated by their pinned
    client cert. ``shared`` carries the singleton PlanJobs/Scheduler/run-lock
    so both apps drive the same state.
    """
    daemon_cfg = load_daemon_cfg(config)
    app = FastAPI(title="fsyncd", version=API_VERSION)
    shared = shared if shared is not None else {}
    run_lock: threading.Lock = shared.setdefault("run_lock", threading.Lock())
    plans: PlanJobs = shared.setdefault("plans", PlanJobs(config))
    scheduler: Scheduler = shared.setdefault(
        "scheduler", Scheduler(daemon_cfg, config, run_lock))
    app.state.scheduler = scheduler
    app.state.daemon_cfg = daemon_cfg
    started = shared.setdefault("started", time.time())

    expected_token = certs.ensure_token() if require_token else None

    def auth(authorization: str | None = Header(default=None)) -> None:
        if expected_token is None:
            return
        # constant-time compare; accept "Bearer <tok>" or the bare token
        supplied = authorization or ""
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        import hmac

        if not hmac.compare_digest(supplied, expected_token):
            raise HTTPException(status_code=401, detail="missing or invalid API token")

    protected = [Depends(auth)]

    @app.get("/v1/status", dependencies=protected)
    def status() -> dict:
        found = discover_run()
        runner = None
        if found and found["running"]:
            prog = found["progress"]
            runner = {"run_id": found["pointer"].get("run_id"),
                      "pid": found["pointer"].get("pid"),
                      "dry_run": prog.get("dry_run"),
                      "started_ts": prog.get("started_ts"),
                      "elapsed": round(time.time() - (prog.get("started_ts") or time.time()), 1)}
        sched = read_schedule(daemon_cfg)
        return {"host": socket.gethostname().split(".")[0],
                "now": time.time(), "daemon_started": started,
                "runner": runner,
                "schedule": {**sched, "next_ts": scheduler.next_ts},
                "last": last_run_summary(),
                "api": API_VERSION}

    @app.get("/v1/profiles", dependencies=protected)
    def profiles() -> dict:
        try:
            _, profs, _ = load_config(config)
        except HomesyncError as e:
            raise HTTPException(status_code=500, detail=str(e))
        return {name: {"paths": p.paths, "conflict": p.conflict,
                       "direction": p.direction, "recursive": p.recursive,
                       "exclude": p.exclude, "merge_jsonl": p.merge_jsonl}
                for name, p in profs.items()}

    def _validate_specs(names: list[str] | None) -> None:
        if not names:
            return
        try:
            _, profs, _ = load_config(config)
        except HomesyncError as e:
            raise HTTPException(status_code=500, detail=str(e))
        for spec in names:
            name, _, direction = str(spec).partition("=")
            if name not in profs:
                raise HTTPException(status_code=400, detail=f"unknown profile: {name}")
            if direction and direction not in DIRECTIONS:
                raise HTTPException(status_code=400,
                                    detail=f"{spec}: direction must be one of {DIRECTIONS}")

    @app.post("/v1/plan", dependencies=protected)
    def plan_start(body: PlanBody = PlanBody()) -> dict:
        _validate_specs(body.profiles)
        return {"job_id": plans.start(body.profiles, body.workers)}

    @app.get("/v1/plan/{job_id}", dependencies=protected)
    def plan_get(job_id: str) -> dict:
        return plans.get(job_id)

    @app.post("/v1/runs", dependencies=protected)
    def run_start(body: RunBody = RunBody()) -> dict:
        _validate_specs(body.profiles)
        selection = (["--all"] if not body.profiles
                     else [a for spec in body.profiles for a in ("--profile", spec)])
        # Serialize check-then-spawn against the scheduler and other POSTs so
        # two callers can't both pass the 409 gate and race two runners.
        if not run_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="another run is being started")
        try:
            found = discover_run()
            if found and found["running"]:
                raise HTTPException(status_code=409,
                                    detail=f"run {found['pointer'].get('run_id')} already active")
            prev = (found or {}).get("pointer", {}).get("run_id")
            proc = spawn_run(selection, dry_run=body.dry_run, config=config)
            now_found = _await_pointer(prev, timeout=20)
            if now_found:
                gc_run_dirs()
                return {"run_id": now_found["pointer"]["run_id"],
                        "running": now_found["running"]}
            # No pointer appeared: the child either deferred cleanly (peer
            # busy/unreachable -> exit 0) or failed (bad args -> exit != 0).
            # Inspect its exit to answer with a precise code, not a blind 502.
            rc = proc.poll()
            if rc == 0:
                raise HTTPException(status_code=409,
                                    detail="run deferred — peer busy or unreachable, nothing to do")
            if rc is not None:
                raise HTTPException(status_code=400,
                                    detail=f"runner exited {rc} without starting — check profiles/config "
                                           f"(see {state_root() / 'daemon-runner.log'})")
            raise HTTPException(status_code=502,
                                detail=f"runner still starting after 20s — see "
                                       f"{state_root() / 'daemon-runner.log'}")
        finally:
            run_lock.release()

    @app.get("/v1/runs/current", dependencies=protected)
    def run_current() -> dict:
        found = discover_run()
        if not found:
            raise HTTPException(status_code=404, detail="no runs yet")
        return {"running": found["running"], "pointer": found["pointer"],
                "progress": found["progress"]}

    @app.get("/v1/runs", dependencies=protected)
    def runs_list(limit: int = 20) -> list[dict]:
        out: list[dict] = []
        runs = state_root() / "runs"
        if not runs.is_dir():
            return out
        for d in sorted((p for p in runs.iterdir() if p.is_dir()), reverse=True)[:max(1, min(limit, 200))]:
            entry: dict[str, Any] = {"run_id": d.name}
            try:
                report = json.loads((d / "report.json").read_text())
                moved, conflicts, errors = report_totals(report)
                entry.update(moved=moved, conflicts=conflicts, errors=errors,
                             dry_run=bool(report.get("dry_run")), finished=report.get("finished"))
            except (OSError, ValueError):
                entry["incomplete"] = True
            out.append(entry)
        return out

    @app.get("/v1/runs/{run_id}", dependencies=protected)
    def run_progress(run_id: str) -> dict:
        d = run_dir_for(run_id)
        try:
            return json.loads((d / "progress.json").read_text())
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="no progress for run")

    @app.get("/v1/runs/{run_id}/report", dependencies=protected)
    def run_report(run_id: str) -> dict:
        d = run_dir_for(run_id)
        try:
            return json.loads((d / "report.json").read_text())
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="no report (run still active?)")

    @app.get("/v1/runs/{run_id}/conflicts", dependencies=protected)
    def run_conflicts(run_id: str) -> dict:
        d = run_dir_for(run_id)
        files = {}
        for f in sorted(d.glob("*/*.conflicts.json")):
            try:
                files[f"{f.parent.name}/{f.name}"] = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
        return {"run_id": run_id, "conflicts": files}

    @app.get("/v1/conflicts", dependencies=protected)
    def latest_conflicts() -> dict:
        runs = state_root() / "runs"
        newest: Path | None = None
        newest_mtime = -1.0
        scanned = 0
        # newest run dir first so the cap keeps the RECENT conflicts, and bail
        # once we have a hit to avoid stat-ing every conflict file ever written
        for d in sorted((p for p in runs.iterdir() if p.is_dir()), reverse=True) if runs.is_dir() else []:
            for f in d.glob("*/*.conflicts.json"):
                scanned += 1
                m = f.stat().st_mtime
                if m > newest_mtime:
                    newest, newest_mtime = f, m
            if newest is not None or scanned >= CONFLICTS_SCAN_CAP:
                break
        if newest is None:
            return {"run_id": None, "conflicts": {}}
        return run_conflicts(newest.parent.parent.name)

    @app.get("/v1/peer", dependencies=protected)
    def peer_status() -> dict:
        try:
            peer, _, _ = load_config(config)
        except HomesyncError as e:
            raise HTTPException(status_code=500, detail=str(e))
        reachable = peer_reachable(peer)
        return {"host": peer.host, "target": peer.target, "reachable": reachable,
                "busy": peer_busy(peer) if reachable else None}

    @app.get("/v1/schedule", dependencies=protected)
    def schedule_get() -> dict:
        return {**read_schedule(daemon_cfg), "next_ts": scheduler.next_ts}

    @app.put("/v1/schedule", dependencies=protected)
    def schedule_put(body: ScheduleBody = ScheduleBody()) -> dict:
        sched = read_schedule(daemon_cfg)
        changed = False
        if body.enabled is not None:
            sched["enabled"] = body.enabled
            changed = True
        if body.interval is not None:
            sched["interval_s"] = max(60, parse_span(body.interval, sched["interval_s"]))
            changed = True
        if body.interval_s is not None:
            sched["interval_s"] = max(60, int(body.interval_s))
            changed = True
        if changed:
            # only re-anchor next_ts when something actually changed, so a
            # repeated no-op PUT can't keep postponing the next run
            write_schedule(sched)
            scheduler.next_ts = (time.time() + sched["interval_s"]) if sched["enabled"] else None
        return {**sched, "next_ts": scheduler.next_ts}

    return app


# --------------------------------------------------------------------------- #
# serving                                                                     #
# --------------------------------------------------------------------------- #

def detect_lan_host(peer_host: str | None) -> str | None:
    """The local address a LAN peer would reach us on.

    Tries the UDP-connect trick against the peer, then a well-known public
    IP (works offline — no packet is sent), then enumerates non-loopback
    IPv4s. A peer-name DNS failure must NOT disable the mTLS listener, so
    every step falls through instead of giving up."""
    for target in ([peer_host] if peer_host else []) + ["192.168.1.1", "10.255.255.255"]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 7))
                addr = s.getsockname()[0]
            if not addr.startswith("127."):
                return addr
        except OSError:
            continue
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if not addr.startswith("127."):
                return addr
    except OSError:
        pass
    return None


async def _serve(daemon_cfg: dict, config: str | None) -> None:
    import uvicorn

    class QuietServer(uvicorn.Server):
        """uvicorn's own signal capture fights itself when two servers run in
        one loop (the last registration wins and the other never exits) —
        shutdown is coordinated by OUR handler below instead."""

        @contextlib.contextmanager
        def capture_signals(self):
            yield

    cert, key = certs.ensure_cert()
    certs.ensure_token()
    shared: dict = {}
    # loopback app requires the local token (defends against other local
    # users); the app instances share PlanJobs/Scheduler/run-lock via `shared`.
    local_app = create_app(config, require_token=True, shared=shared)
    scheduler = shared["scheduler"]
    servers: list[uvicorn.Server] = []

    local = uvicorn.Config(local_app, host="127.0.0.1", port=daemon_cfg["port"],
                           ssl_certfile=str(cert), ssl_keyfile=str(key),
                           timeout_graceful_shutdown=5,  # never hang on idle keep-alives
                           log_level="warning")
    servers.append(QuietServer(local))

    bundle = certs.rebuild_bundle()
    lan_host = daemon_cfg["lan_host"]
    if lan_host == "auto":
        peer_host = None
        try:
            peer, _, _ = load_config(config)
            peer_host = peer.host
        except HomesyncError:
            pass
        lan_host = detect_lan_host(peer_host)
    if bundle and lan_host:
        # peers authenticate by their pinned client cert (CERT_REQUIRED), so
        # this app does NOT require the local token
        lan_app = create_app(config, require_token=False, shared=shared)
        lan = uvicorn.Config(lan_app, host=lan_host, port=daemon_cfg["port"],
                             ssl_certfile=str(cert), ssl_keyfile=str(key),
                             ssl_ca_certs=str(bundle),
                             ssl_cert_reqs=ssl.CERT_REQUIRED,
                             timeout_graceful_shutdown=5,
                             log_level="warning")
        servers.append(QuietServer(lan))
        print(f"fsyncd: mTLS listener on {lan_host}:{daemon_cfg['port']} "
              f"(trusted peers: {', '.join(certs.trusted_peers())})", flush=True)
    else:
        why = "no trusted peers" if not bundle else "no LAN address detected"
        print(f"fsyncd: mTLS listener disabled ({why})", flush=True)
    print(f"fsyncd: TLS listener on 127.0.0.1:{daemon_cfg['port']} (token-gated)", flush=True)

    sched_task = asyncio.create_task(scheduler.run())
    server_tasks = [asyncio.create_task(s.serve()) for s in servers]

    def _stop() -> None:
        for s in servers:
            s.should_exit = True
        sched_task.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop)

    await asyncio.gather(*server_tasks, return_exceptions=True)
    sched_task.cancel()
    await asyncio.gather(sched_task, return_exceptions=True)


def run_daemon(config: str | None = None) -> int:
    daemon_cfg = load_daemon_cfg(config)
    try:
        asyncio.run(_serve(daemon_cfg, config))
    except KeyboardInterrupt:
        pass
    return 0


# --------------------------------------------------------------------------- #
# lifecycle CLI (fsync daemon ...)                                            #
# --------------------------------------------------------------------------- #

UNIT_NAME = "fsync-daemon"


def render_daemon_unit(python: str) -> str:
    return f"""[Unit]
Description=fsyncd — fsync home-sync backend (REST over TLS, scheduler)
After=network.target

[Service]
Type=exec
Restart=always
RestartSec=5
Nice=10
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
ExecStart={python} -m fsync.cli daemon run

[Install]
WantedBy=default.target
"""


def _systemctl(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *argv], capture_output=True, text=True)


def cmd_daemon(args) -> int:
    action = getattr(args, "action", None)

    if action == "run":
        return run_daemon(getattr(args, "config", None))

    if action == "cert":
        cert, _ = certs.ensure_cert()
        if getattr(args, "show", False):
            print(cert.read_text(), end="")
        else:
            print(f"{cert}\n  SHA256 {certs.cert_fingerprint(cert)}")
        return 0

    if action == "install":
        certs.ensure_cert()
        unit_dir = Path("~/.config/systemd/user").expanduser()
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / f"{UNIT_NAME}.service").write_text(render_daemon_unit(sys.executable))
        _systemctl("daemon-reload")
        en = _systemctl("enable", "--now", f"{UNIT_NAME}.service")
        if en.returncode != 0:
            print(f"enable failed: {en.stderr.strip()}", file=sys.stderr)
            return 1
        # the daemon's scheduler replaces the P3 systemd timer
        if _systemctl("is-enabled", "fsync-sync.timer").returncode == 0:
            _systemctl("disable", "--now", "fsync-sync.timer")
            print("retired fsync-sync.timer — fsyncd schedules runs now")
        cert, _ = certs.ensure_cert()
        print(f"fsync-daemon running (port {load_daemon_cfg(getattr(args, 'config', None))['port']})")
        print(f"cert: SHA256 {certs.cert_fingerprint(cert)}")
        print("next: exchange certs with the peer -> fsync daemon trust <ssh-host>")
        return 0

    if action == "remove":
        _systemctl("disable", "--now", f"{UNIT_NAME}.service")
        unit = Path("~/.config/systemd/user").expanduser() / f"{UNIT_NAME}.service"
        unit.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        print("fsync-daemon removed (TLS material kept in ~/.config/fsync/tls)")
        return 0

    if action == "status":
        state = _systemctl("is-active", f"{UNIT_NAME}.service").stdout.strip()
        print(f"service: {state}")
        try:
            from .client import FsyncClient

            st = FsyncClient(port=load_daemon_cfg(getattr(args, "config", None))["port"]).status()
            runner = st.get("runner")
            sched = st.get("schedule") or {}
            nxt = sched.get("next_ts")
            print(f"api: ok (host {st.get('host')})")
            print(f"runner: {'run ' + runner['run_id'] + ' active' if runner else 'idle'}")
            print(f"schedule: {'enabled' if sched.get('enabled') else 'DISABLED'}, "
                  f"every {sched.get('interval_s', 0) // 60}min"
                  + (f", next {time.strftime('%H:%M', time.localtime(nxt))}" if nxt else ""))
            last = st.get("last")
            if last:
                print(f"last: {last['run_id']} — {last['moved']} moved, "
                      f"{last['conflicts']} held, {last['errors']} errors")
            print(f"trusted peers: {', '.join(certs.trusted_peers()) or 'none'}")
        except Exception as e:  # status is diagnostics: show, don't crash
            print(f"api: unreachable ({e})")
        return 0

    if action == "trust":
        host = getattr(args, "host", None)
        if not host:
            print("usage: fsync daemon trust <ssh-host> [--name NAME]", file=sys.stderr)
            return 2
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
             "~/.local/bin/fsync daemon cert --show"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or "BEGIN CERTIFICATE" not in proc.stdout:
            print(f"could not fetch peer cert over ssh: {proc.stderr.strip()[-200:]}",
                  file=sys.stderr)
            return 1
        name = getattr(args, "name", None) or host.split("@")[-1].split(".")[0]
        dest = certs.trust_peer(name, proc.stdout)
        print(f"pinned {name}: SHA256 {certs.cert_fingerprint(dest)}")
        print("restart the daemon to (re)open the mTLS listener: "
              f"systemctl --user restart {UNIT_NAME}")
        print(f"reciprocal on the peer: ssh {host} '~/.local/bin/fsync daemon trust "
              f"{socket.gethostname().split('.')[0]}.lan'")
        return 0

    print("usage: fsync daemon {run|install|remove|status|trust|cert}", file=sys.stderr)
    return 2
