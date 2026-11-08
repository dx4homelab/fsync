"""Hub-side state for the star topology (P7.1, docs/hub-sync.md R36/R41).

One sqlite file under the hub root (``~/.fsync/hub/state.sqlite``) holds:

    leases      one row per profile: which spoke currently converges it
                (TTL-bounded; a crashed spoke's lease simply expires — R36)
    spoke_runs  (host, profile) → the last run that spoke completed against
                the hub, with a small JSON summary (R41: "which box is behind")
    meta        schema version + bookkeeping

stdlib only (sqlite3) so it fits the deployed .pyz (R40) and the daemon venv
alike; the hub daemon and the hub box's own local spoke run open the same
file — sqlite's locking makes the lease handshake atomic across processes.

P7.2 adds ``paths`` / ``journal`` / ``cursors`` / ``holds`` here; the schema
is versioned so that migration is a plain ``CREATE TABLE IF NOT EXISTS``.
"""

from __future__ import annotations

import json
import secrets
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_LEASE_TTL = 900          # seconds; renewed at TTL/3 while a profile runs
MIN_LEASE_TTL = 30
MAX_LEASE_TTL = 6 * 3600


class LeaseHeld(RuntimeError):
    """Another spoke holds the lease for this profile (not expired)."""

    def __init__(self, profile: str, holder: str, run_id: str | None, expires: float):
        self.profile, self.holder, self.run_id, self.expires = profile, holder, run_id, expires
        left = max(0, int(expires - time.time()))
        super().__init__(f"profile '{profile}' is being synced by {holder}"
                         f"{f' (run {run_id})' if run_id else ''} — lease expires in {left}s")


class LeaseLost(RuntimeError):
    """A renew/release named a lease that is no longer current (expired and
    taken over, or never existed)."""


def _clamp_ttl(ttl: int | None) -> int:
    try:
        t = int(ttl or DEFAULT_LEASE_TTL)
    except (TypeError, ValueError):
        t = DEFAULT_LEASE_TTL
    return max(MIN_LEASE_TTL, min(MAX_LEASE_TTL, t))


class HubState:
    """Thin, transactional wrapper over the hub sqlite file."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False,
                                     isolation_level=None)  # explicit transactions below
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    # ------------------------------------------------------------------ setup

    def _init_schema(self) -> None:
        # executescript commits on its own (and would break an open BEGIN);
        # the connection is in autocommit mode, so no wrapper here.
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leases (
                    profile  TEXT PRIMARY KEY,
                    host     TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    run_id   TEXT,
                    acquired REAL NOT NULL,
                    expires  REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS spoke_runs (
                    host     TEXT NOT NULL,
                    profile  TEXT NOT NULL,
                    run_id   TEXT NOT NULL,
                    finished REAL NOT NULL,
                    summary  TEXT NOT NULL,
                    PRIMARY KEY (host, profile)
                );
            """)
            self._conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)",
                               (str(SCHEMA_VERSION),))
            self._conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('created', ?)",
                               (str(time.time()),))

    class _Tx:
        def __init__(self, st: "HubState"):
            self.st = st

        def __enter__(self):
            self.st._lock.acquire()
            self.st._conn.execute("BEGIN IMMEDIATE")
            return self.st._conn

        def __exit__(self, et, ev, tb):
            try:
                if et is None:
                    self.st._conn.execute("COMMIT")
                else:
                    self.st._conn.execute("ROLLBACK")
            finally:
                self.st._lock.release()
            return False

    def _tx(self) -> "_Tx":
        return HubState._Tx(self)

    def close(self) -> None:
        self._conn.close()

    # ----------------------------------------------------------------- leases

    def acquire(self, profile: str, host: str, run_id: str | None = None,
                ttl: int | None = None, now: float | None = None) -> dict[str, Any]:
        """Grant ``host`` the lease on ``profile`` (R36).

        Free, expired, or already ours (same host + run_id) → granted/renewed.
        Held by someone else and not expired → ``LeaseHeld``. No manual unlock
        exists by design: the TTL is the unlock."""
        now = now or time.time()
        ttl_s = _clamp_ttl(ttl)
        with self._tx() as c:
            row = c.execute("SELECT * FROM leases WHERE profile = ?", (profile,)).fetchone()
            if row is not None and row["expires"] > now and not (
                    row["host"] == host and (run_id is None or row["run_id"] == run_id)):
                raise LeaseHeld(profile, row["host"], row["run_id"], row["expires"])
            lease_id = (row["lease_id"] if row is not None and row["expires"] > now
                        else secrets.token_hex(8))
            c.execute(
                "INSERT INTO leases(profile, host, lease_id, run_id, acquired, expires) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(profile) DO UPDATE SET "
                "host=excluded.host, lease_id=excluded.lease_id, run_id=excluded.run_id, "
                "acquired=excluded.acquired, expires=excluded.expires",
                (profile, host, lease_id, run_id, now, now + ttl_s))
        return {"lease_id": lease_id, "profile": profile, "host": host, "run_id": run_id,
                "expires": now + ttl_s, "ttl": ttl_s}

    def renew(self, profile: str, lease_id: str, ttl: int | None = None,
              now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        ttl_s = _clamp_ttl(ttl)
        with self._tx() as c:
            row = c.execute("SELECT * FROM leases WHERE profile = ? AND lease_id = ?",
                            (profile, lease_id)).fetchone()
            if row is None or row["expires"] <= now:
                raise LeaseLost(f"lease {lease_id} on '{profile}' is no longer current")
            c.execute("UPDATE leases SET expires = ? WHERE profile = ? AND lease_id = ?",
                      (now + ttl_s, profile, lease_id))
            return {"lease_id": lease_id, "profile": profile, "host": row["host"],
                    "run_id": row["run_id"], "expires": now + ttl_s, "ttl": ttl_s}

    def release(self, profile: str, lease_id: str) -> bool:
        """Drop the lease; False when it was not ours any more (already
        expired and re-granted) — harmless, the new holder keeps its lease."""
        with self._tx() as c:
            cur = c.execute("DELETE FROM leases WHERE profile = ? AND lease_id = ?",
                            (profile, lease_id))
            return cur.rowcount > 0

    def leases(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        now = now or time.time()
        rows = self._conn.execute("SELECT * FROM leases WHERE expires > ?", (now,)).fetchall()
        return {r["profile"]: {"host": r["host"], "run_id": r["run_id"], "lease_id": r["lease_id"],
                               "acquired": r["acquired"], "expires": r["expires"]} for r in rows}

    # ------------------------------------------------------------ spoke runs

    def record_run(self, host: str, profile: str, run_id: str, summary: dict[str, Any],
                   now: float | None = None) -> None:
        """Remember the last completed spoke run per (host, profile) — the
        P7.1 seed of the P7.2 cursor table (R34/R41)."""
        now = now or time.time()
        with self._tx() as c:
            c.execute(
                "INSERT INTO spoke_runs(host, profile, run_id, finished, summary) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(host, profile) DO UPDATE SET "
                "run_id=excluded.run_id, finished=excluded.finished, summary=excluded.summary",
                (host, profile, run_id, now, json.dumps(summary, sort_keys=True)))

    def status(self, now: float | None = None) -> dict[str, Any]:
        """Per-profile lease + per-spoke last run; the body of `fsync hub status`."""
        now = now or time.time()
        leases = self.leases(now)
        profiles: dict[str, Any] = {}
        hosts: dict[str, Any] = {}
        for r in self._conn.execute("SELECT * FROM spoke_runs ORDER BY profile, host"):
            try:
                summary = json.loads(r["summary"])
            except ValueError:
                summary = {}
            entry = {"run_id": r["run_id"], "finished": r["finished"],
                     "age_s": max(0, int(now - r["finished"])), "summary": summary}
            profiles.setdefault(r["profile"], {"lease": None, "spokes": {}})["spokes"][r["host"]] = entry
            h = hosts.setdefault(r["host"], {"profiles": 0, "last_seen": 0.0})
            h["profiles"] += 1
            h["last_seen"] = max(h["last_seen"], r["finished"])
        for prof, lease in leases.items():
            profiles.setdefault(prof, {"lease": None, "spokes": {}})["lease"] = lease
        return {"now": now, "schema": SCHEMA_VERSION, "profiles": profiles, "hosts": hosts,
                "leases": leases}


# --------------------------------------------------------------------------- #
# control-plane facade used by the spoke run                                  #
# --------------------------------------------------------------------------- #

def local_host_name() -> str:
    return socket.gethostname().split(".")[0]


class Lease:
    """Context manager: hold a profile lease for the duration of a transfer,
    renewing it in the background at TTL/3 so a long rsync leg never outlives
    it (R36). Renewal failures are remembered, not raised mid-transfer — the
    caller inspects ``lost`` afterwards (the hub will have let someone else in
    only if we really were presumed dead)."""

    def __init__(self, control: "HubControl", profile: str, host: str, run_id: str,
                 ttl: int = DEFAULT_LEASE_TTL):
        self.control, self.profile, self.host, self.run_id = control, profile, host, run_id
        self.ttl = _clamp_ttl(ttl)
        self.lease_id: str | None = None
        self.lost: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "Lease":
        granted = self.control.acquire(self.profile, self.host, self.run_id, self.ttl)
        self.lease_id = granted["lease_id"]
        self._thread = threading.Thread(target=self._renewer, name=f"lease-{self.profile}",
                                        daemon=True)
        self._thread.start()
        return self

    def _renewer(self) -> None:
        while not self._stop.wait(self.ttl / 3):
            try:
                self.control.renew(self.profile, self.lease_id or "", self.ttl)
            except Exception as e:  # noqa: BLE001 — recorded, never kills the transfer
                self.lost = f"{type(e).__name__}: {e}"
                return

    def __exit__(self, et, ev, tb) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.lease_id:
            try:
                self.control.release(self.profile, self.lease_id)
            except Exception:  # noqa: BLE001 — releasing is best effort; TTL is the backstop
                pass
        return False


class HubControl:
    """What a spoke needs from the hub's control plane (R37). Two backends:
    ``LocalHubControl`` (hub role on this very box → sqlite directly) and
    ``hubclient.RemoteHubControl`` (TLS to the hub daemon)."""

    def acquire(self, profile: str, host: str, run_id: str | None, ttl: int) -> dict: ...
    def renew(self, profile: str, lease_id: str, ttl: int) -> dict: ...
    def release(self, profile: str, lease_id: str) -> bool: ...
    def record_run(self, host: str, profile: str, run_id: str, summary: dict) -> None: ...
    def status(self) -> dict: ...

    def lease(self, profile: str, host: str, run_id: str, ttl: int = DEFAULT_LEASE_TTL) -> Lease:
        return Lease(self, profile, host, run_id, ttl)


class LocalHubControl(HubControl):
    def __init__(self, state: HubState):
        self.state = state

    def acquire(self, profile, host, run_id, ttl):
        return self.state.acquire(profile, host, run_id, ttl)

    def renew(self, profile, lease_id, ttl):
        return self.state.renew(profile, lease_id, ttl)

    def release(self, profile, lease_id):
        return self.state.release(profile, lease_id)

    def record_run(self, host, profile, run_id, summary):
        self.state.record_run(host, profile, run_id, summary)

    def status(self):
        return self.state.status()
