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

SCHEMA_VERSION = 2
DEFAULT_LEASE_TTL = 900
MTIME_TOLERANCE = 1.0            # seconds; rsync -a preserves mtimes, filesystems round
INDEXED_KEY = "indexed:"         # meta key prefix: profile has an authoritative paths table
JOURNAL_FLOOR_KEY = "journal_floor:"  # meta key prefix: highest seq removed by gc          # seconds; renewed at TTL/3 while a profile runs
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
                -- P7.2 (R32-R35): the hub's view of every path, the change journal,
                -- per-spoke cursors and conflict: review holds.
                CREATE TABLE IF NOT EXISTS paths (
                    profile    TEXT NOT NULL,
                    path       TEXT NOT NULL,
                    hash       TEXT,
                    size       INTEGER,
                    mtime      REAL,
                    seq        INTEGER NOT NULL,
                    origin     TEXT,
                    deleted_at REAL,                  -- NULL = live, else a tombstone (R33)
                    PRIMARY KEY (profile, path)
                );
                CREATE TABLE IF NOT EXISTS journal (
                    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile TEXT NOT NULL,
                    path    TEXT NOT NULL,
                    op      TEXT NOT NULL,            -- put | del | hold
                    hash    TEXT,
                    size    INTEGER,
                    mtime   REAL,
                    origin  TEXT,
                    ts      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS journal_profile_seq ON journal (profile, seq);
                CREATE TABLE IF NOT EXISTS cursors (
                    host        TEXT NOT NULL,
                    profile     TEXT NOT NULL,
                    last_seq    INTEGER NOT NULL,
                    last_run_id TEXT,
                    last_seen   REAL NOT NULL,
                    PRIMARY KEY (host, profile)
                );
                -- P7.3 (R39): one row per (kind, profile, repo/checkout, producer host) —
                -- the meta a receiver applies; bytes live in <root>/git/*.git or <root>/svn/.
                CREATE TABLE IF NOT EXISTS kind_snapshots (
                    kind    TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    rel     TEXT NOT NULL,
                    host    TEXT NOT NULL,
                    name    TEXT,
                    meta    TEXT NOT NULL,
                    ts      REAL NOT NULL,
                    PRIMARY KEY (kind, profile, rel, host)
                );
                CREATE TABLE IF NOT EXISTS holds (
                    profile         TEXT NOT NULL,
                    path            TEXT NOT NULL,
                    hub_hash        TEXT,
                    incoming_hash   TEXT,
                    incoming_origin TEXT,
                    incoming_mtime  REAL,
                    since           REAL NOT NULL,
                    PRIMARY KEY (profile, path)
                );
            """)
            self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)",
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

    # ------------------------------------------------------- paths / journal

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _meta_set(self, c, key: str, value: str) -> None:
        c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                  "value = excluded.value", (key, value))

    def is_indexed(self, profile: str) -> bool:
        """True once the profile's paths table is authoritative (`hub init` on an
        empty tree, or `hub index` over an existing one). A spoke refuses a
        full-mode run against an unindexed profile — it would re-push the world."""
        return self._meta_get(INDEXED_KEY + profile) is not None

    def mark_indexed(self, profile: str, now: float | None = None) -> None:
        with self._tx() as c:
            self._meta_set(c, INDEXED_KEY + profile, str(now or time.time()))

    def head_seq(self, profile: str) -> int:
        row = self._conn.execute("SELECT MAX(seq) AS s FROM journal WHERE profile = ?",
                                 (profile,)).fetchone()
        return int(row["s"] or 0)

    def journal_floor(self, profile: str) -> int:
        """Highest seq gc has removed for this profile (0 = journal complete)."""
        v = self._meta_get(JOURNAL_FLOOR_KEY + profile)
        return int(v) if v else 0

    def state(self, profile: str, prefix: str = "") -> dict[str, Any]:
        """Current hub view of ``profile`` under ``prefix`` — compact lists so a
        100k-path profile stays a few MB: live ``paths[p] = [size, mtime, hash, seq]``,
        ``tombstones[p] = [deleted_at, seq]``."""
        like = prefix.rstrip("/") + "/%" if prefix and prefix != "." else "%"
        live: dict[str, list] = {}
        tomb: dict[str, list] = {}
        for r in self._conn.execute(
                "SELECT path, hash, size, mtime, seq, deleted_at FROM paths "
                "WHERE profile = ? AND path LIKE ?", (profile, like)):
            if r["deleted_at"] is None:
                live[r["path"]] = [r["size"], r["mtime"], r["hash"], r["seq"]]
            else:
                tomb[r["path"]] = [r["deleted_at"], r["seq"]]
        return {"profile": profile, "prefix": prefix, "seq": self.head_seq(profile),
                "indexed": self.is_indexed(profile), "paths": live, "tombstones": tomb}

    def journal_since(self, profile: str, since: int, prefix: str = "",
                      limit: int = 200000) -> dict[str, Any]:
        """Entries after ``since`` (R34). ``complete`` is False when gc removed
        entries the caller has not seen — then it must fall back to ``state``."""
        like = prefix.rstrip("/") + "/%" if prefix and prefix != "." else "%"
        rows = self._conn.execute(
            "SELECT seq, path, op, hash, size, mtime, origin, ts FROM journal "
            "WHERE profile = ? AND seq > ? AND path LIKE ? ORDER BY seq LIMIT ?",
            (profile, since, like, limit)).fetchall()
        entries = [[r["seq"], r["path"], r["op"], r["hash"], r["size"], r["mtime"], r["origin"], r["ts"]]
                   for r in rows]
        head = self.head_seq(profile)
        return {"profile": profile, "since": since, "seq": head, "entries": entries,
                "complete": since >= self.journal_floor(profile) and len(entries) < limit,
                "indexed": self.is_indexed(profile)}

    def cursors(self) -> dict[str, dict[str, dict[str, Any]]]:
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for r in self._conn.execute("SELECT * FROM cursors"):
            out.setdefault(r["profile"], {})[r["host"]] = {
                "last_seq": r["last_seq"], "last_run_id": r["last_run_id"], "last_seen": r["last_seen"]}
        return out

    def holds(self, profile: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM holds" + (" WHERE profile = ?" if profile else "") + " ORDER BY profile, path"
        rows = self._conn.execute(q, (profile,) if profile else ()).fetchall()
        return [dict(r) for r in rows]

    def release_hold(self, profile: str, path: str) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM holds WHERE profile = ? AND path = ?",
                             (profile, path)).rowcount > 0

    def _journal(self, c, profile: str, path: str, op: str, hash_: str | None, size: int | None,
                 mtime: float | None, origin: str, now: float) -> int:
        cur = c.execute("INSERT INTO journal(profile, path, op, hash, size, mtime, origin, ts) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (profile, path, op, hash_, size, mtime, origin, now))
        return int(cur.lastrowid)

    def _upsert_path(self, c, profile: str, path: str, hash_: str | None, size: int | None,
                     mtime: float | None, seq: int, origin: str, deleted_at: float | None) -> None:
        c.execute("INSERT INTO paths(profile, path, hash, size, mtime, seq, origin, deleted_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(profile, path) DO UPDATE SET "
                  "hash=excluded.hash, size=excluded.size, mtime=excluded.mtime, seq=excluded.seq, "
                  "origin=excluded.origin, deleted_at=excluded.deleted_at",
                  (profile, path, hash_, size, mtime, seq, origin, deleted_at))

    def commit(self, profile: str, host: str, run_id: str, lease_id: str | None,
               puts: list[dict], dels: list[dict], holds: list[dict], *,
               tree_root: Path, backup_dir: Path, now: float | None = None) -> dict[str, Any]:
        """Accept a spoke's finished transfer into the hub's state (R33/R34/R35).

        ``puts`` were rsync'd into ``tree_root`` already: each is verified against
        the file (size, mtime within tolerance) before it is journaled — a
        mismatch is ``rejected`` and the spoke re-plans it next run. ``dels`` move
        the tree file to ``backup_dir`` (never rm) and become tombstones. ``holds``
        record a conflict the spoke's policy refused to resolve. The caller's
        lease must be current when one is given (the hub box's own tools pass
        None). Returns the new head seq."""
        now = now or time.time()
        rejected: list[dict[str, str]] = []
        accepted_puts: list[str] = []
        accepted_dels: list[str] = []
        moved: list[tuple[Path, Path]] = []
        # file work first (outside the transaction): a failed move is a rejection,
        # not a half-recorded tombstone
        del_ok: list[dict] = []
        for d in dels:
            path = str(d.get("path") or "")
            if not path or path.startswith("/") or ".." in path.split("/"):
                rejected.append({"path": path, "reason": "bad path"})
                continue
            src = tree_root / path
            if src.is_file() or src.is_symlink():
                dst = backup_dir / path
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        src.replace(dst)
                    except OSError:
                        import shutil
                        shutil.move(str(src), str(dst))
                    moved.append((src, dst))
                except OSError as e:
                    rejected.append({"path": path, "reason": f"backup move failed: {e}"})
                    continue
            del_ok.append(d)
        put_ok: list[dict] = []
        for p in puts:
            path = str(p.get("path") or "")
            if not path or path.startswith("/") or ".." in path.split("/"):
                rejected.append({"path": path, "reason": "bad path"})
                continue
            f = tree_root / path
            try:
                st = f.stat()
            except OSError:
                rejected.append({"path": path, "reason": "not in hub tree"})
                continue
            size, mtime = p.get("size"), p.get("mtime")
            if size is not None and int(size) != st.st_size:
                rejected.append({"path": path, "reason": f"size {st.st_size} != {size}"})
                continue
            if mtime is not None and abs(float(mtime) - st.st_mtime) > MTIME_TOLERANCE:
                rejected.append({"path": path, "reason": "mtime differs"})
                continue
            put_ok.append({**p, "size": st.st_size, "mtime": st.st_mtime})
        with self._tx() as c:
            if lease_id is not None:
                row = c.execute("SELECT * FROM leases WHERE profile = ? AND lease_id = ?",
                                (profile, lease_id)).fetchone()
                if row is None or row["expires"] <= now:
                    raise LeaseLost(f"commit on '{profile}' with a lease that is no longer current")
            for p in put_ok:
                seq = self._journal(c, profile, p["path"], "put", p.get("hash"), p["size"], p["mtime"],
                                    host, now)
                self._upsert_path(c, profile, p["path"], p.get("hash"), p["size"], p["mtime"], seq,
                                  host, None)
                c.execute("DELETE FROM holds WHERE profile = ? AND path = ?", (profile, p["path"]))
                accepted_puts.append(p["path"])
            for d in del_ok:
                seq = self._journal(c, profile, d["path"], "del", d.get("hash"), None, None, host, now)
                self._upsert_path(c, profile, d["path"], d.get("hash"), None, None, seq, host, now)
                c.execute("DELETE FROM holds WHERE profile = ? AND path = ?", (profile, d["path"]))
                accepted_dels.append(d["path"])
            for h in holds:
                path = str(h.get("path") or "")
                if not path:
                    continue
                self._journal(c, profile, path, "hold", h.get("incoming_hash"), None,
                              h.get("incoming_mtime"), host, now)
                c.execute("INSERT INTO holds(profile, path, hub_hash, incoming_hash, incoming_origin, "
                          "incoming_mtime, since) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(profile, path) "
                          "DO UPDATE SET hub_hash=excluded.hub_hash, incoming_hash=excluded.incoming_hash, "
                          "incoming_origin=excluded.incoming_origin, incoming_mtime=excluded.incoming_mtime",
                          (profile, path, h.get("hub_hash"), h.get("incoming_hash"), host,
                           h.get("incoming_mtime"), now))
            head = c.execute("SELECT MAX(seq) AS s FROM journal WHERE profile = ?",
                             (profile,)).fetchone()["s"] or 0
            c.execute("INSERT INTO cursors(host, profile, last_seq, last_run_id, last_seen) "
                      "VALUES (?, ?, ?, ?, ?) ON CONFLICT(host, profile) DO UPDATE SET "
                      "last_seq=excluded.last_seq, last_run_id=excluded.last_run_id, "
                      "last_seen=excluded.last_seen", (host, profile, int(head), run_id, now))
        return {"seq_to": int(head), "accepted_puts": accepted_puts, "accepted_dels": accepted_dels,
                "rejected": rejected, "holds": len(holds), "moved_to_backup": len(moved)}

    def index_tree(self, profile: str, records: list[dict], *, origin: str = "hub-index",
                   now: float | None = None) -> dict[str, int]:
        """Make the paths table agree with an index of the profile tree (bootstrap
        of a tree that predates P7.2, or repair): rows missing/differing → put,
        live rows whose file is gone → tombstone. Marks the profile indexed."""
        now = now or time.time()
        seen: set[str] = set()
        added = changed = tombstoned = unchanged = 0
        with self._tx() as c:
            existing = {r["path"]: r for r in c.execute(
                "SELECT path, hash, size, mtime, deleted_at FROM paths WHERE profile = ?", (profile,))}
            for rec in records:
                path = rec.get("path")
                if not path:
                    continue
                seen.add(path)
                row = existing.get(path)
                same = (row is not None and row["deleted_at"] is None
                        and (row["hash"] == rec.get("hash") if row["hash"] and rec.get("hash")
                             else (row["size"] == rec.get("size")
                                   and abs((row["mtime"] or 0) - (rec.get("mtime") or 0)) <= MTIME_TOLERANCE)))
                if same:
                    unchanged += 1
                    continue
                seq = self._journal(c, profile, path, "put", rec.get("hash"), rec.get("size"),
                                    rec.get("mtime"), origin, now)
                self._upsert_path(c, profile, path, rec.get("hash"), rec.get("size"), rec.get("mtime"),
                                  seq, origin, None)
                if row is None:
                    added += 1
                else:
                    changed += 1
            for path, row in existing.items():
                if path in seen or row["deleted_at"] is not None:
                    continue
                seq = self._journal(c, profile, path, "del", row["hash"], None, None, origin, now)
                self._upsert_path(c, profile, path, row["hash"], None, None, seq, origin, now)
                tombstoned += 1
            self._meta_set(c, INDEXED_KEY + profile, str(now))
        return {"added": added, "changed": changed, "tombstoned": tombstoned, "unchanged": unchanged,
                "seq": self.head_seq(profile)}

    def gc(self, *, tombstone_days: float = 90, journal_days: float = 90,
           now: float | None = None, dry_run: bool = False) -> dict[str, int]:
        """Compact tombstones and journal entries older than the retention
        (R33). The journal floor per profile is remembered so a spoke whose
        cursor predates it falls back to a full state fetch."""
        now = now or time.time()
        t_cut, j_cut = now - tombstone_days * 86400, now - journal_days * 86400
        n_t = self._conn.execute("SELECT COUNT(*) AS n FROM paths WHERE deleted_at IS NOT NULL "
                                 "AND deleted_at < ?", (t_cut,)).fetchone()["n"]
        n_j = self._conn.execute("SELECT COUNT(*) AS n FROM journal WHERE ts < ?",
                                 (j_cut,)).fetchone()["n"]
        if not dry_run:
            with self._tx() as c:
                for r in c.execute("SELECT profile, MAX(seq) AS s FROM journal WHERE ts < ? "
                                   "GROUP BY profile", (j_cut,)).fetchall():
                    self._meta_set(c, JOURNAL_FLOOR_KEY + r["profile"], str(int(r["s"])))
                c.execute("DELETE FROM journal WHERE ts < ?", (j_cut,))
                c.execute("DELETE FROM paths WHERE deleted_at IS NOT NULL AND deleted_at < ?", (t_cut,))
        return {"tombstones": int(n_t), "journal": int(n_j)}

    # ------------------------------------------------------- kind snapshots

    def put_kind_snapshot(self, kind: str, profile: str, rel: str, host: str, meta: dict,
                          now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        with self._tx() as c:
            c.execute("INSERT INTO kind_snapshots(kind, profile, rel, host, name, meta, ts) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(kind, profile, rel, host) DO UPDATE SET "
                      "name=excluded.name, meta=excluded.meta, ts=excluded.ts",
                      (kind, profile, rel, host, meta.get("name"), json.dumps(meta, sort_keys=True), now))
        return {"kind": kind, "profile": profile, "rel": rel, "host": host, "ts": now}

    def kind_snapshots(self, kind: str, profile: str | None = None,
                       rel: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM kind_snapshots WHERE kind = ?"
        args: list = [kind]
        if profile:
            q += " AND profile = ?"; args.append(profile)
        if rel:
            q += " AND rel = ?"; args.append(rel)
        out = []
        for r in self._conn.execute(q + " ORDER BY rel, ts DESC", args):
            try:
                meta = json.loads(r["meta"])
            except ValueError:
                meta = {}
            out.append({"kind": r["kind"], "profile": r["profile"], "rel": r["rel"], "host": r["host"],
                        "name": r["name"], "ts": r["ts"], "meta": meta})
        return out

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
        # P7.2 state per profile: head seq, live/tombstone counts, holds, cursors
        for r in self._conn.execute(
                "SELECT profile, SUM(deleted_at IS NULL) AS live, SUM(deleted_at IS NOT NULL) AS tomb "
                "FROM paths GROUP BY profile"):
            e = profiles.setdefault(r["profile"], {"lease": None, "spokes": {}})
            e["live"], e["tombstones"] = int(r["live"] or 0), int(r["tomb"] or 0)
        for r in self._conn.execute("SELECT profile, MAX(seq) AS s FROM journal GROUP BY profile"):
            profiles.setdefault(r["profile"], {"lease": None, "spokes": {}})["seq"] = int(r["s"] or 0)
        for r in self._conn.execute("SELECT profile, COUNT(*) AS n FROM holds GROUP BY profile"):
            profiles.setdefault(r["profile"], {"lease": None, "spokes": {}})["holds"] = int(r["n"])
        for prof, by_host in self.cursors().items():
            e = profiles.setdefault(prof, {"lease": None, "spokes": {}})
            e["cursors"] = by_host
        for prof, e in profiles.items():
            e["indexed"] = self.is_indexed(prof)
        kinds: dict[str, Any] = {}
        for r in self._conn.execute(
                "SELECT kind, profile, host, COUNT(*) AS n, MAX(ts) AS ts FROM kind_snapshots "
                "GROUP BY kind, profile, host"):
            kinds.setdefault(r["profile"], {"kind": r["kind"], "hosts": {}})["hosts"][r["host"]] = {
                "snapshots": int(r["n"]), "latest": r["ts"], "age_s": max(0, int(now - r["ts"]))}
        return {"now": now, "schema": SCHEMA_VERSION, "profiles": profiles, "hosts": hosts,
                "leases": leases, "kinds": kinds}


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
    # P7.2
    def state(self, profile: str, prefix: str = "") -> dict: ...
    def journal(self, profile: str, since: int, prefix: str = "") -> dict: ...
    def commit(self, profile: str, host: str, run_id: str, lease_id: str | None,
               puts: list[dict], dels: list[dict], holds: list[dict]) -> dict: ...
    def holds(self, profile: str | None = None) -> list[dict]: ...
    # P7.3
    def put_kind_snapshot(self, kind: str, profile: str, rel: str, host: str, meta: dict) -> dict: ...
    def kind_snapshots(self, kind: str, profile: str | None = None, rel: str | None = None) -> list[dict]: ...

    def lease(self, profile: str, host: str, run_id: str, ttl: int = DEFAULT_LEASE_TTL) -> Lease:
        return Lease(self, profile, host, run_id, ttl)


class LocalHubControl(HubControl):
    """Direct sqlite + direct tree access: the hub box's own tools and the
    one-box test fixture. ``root`` is the hub root (trees + backups)."""

    def __init__(self, state: HubState, root: str | Path | None = None):
        self.state_db = state
        self.root = Path(root).expanduser() if root else state.path.parent

    def acquire(self, profile, host, run_id, ttl):
        return self.state_db.acquire(profile, host, run_id, ttl)

    def renew(self, profile, lease_id, ttl):
        return self.state_db.renew(profile, lease_id, ttl)

    def release(self, profile, lease_id):
        return self.state_db.release(profile, lease_id)

    def record_run(self, host, profile, run_id, summary):
        self.state_db.record_run(host, profile, run_id, summary)

    def status(self):
        return self.state_db.status()

    def state(self, profile, prefix=""):
        return self.state_db.state(profile, prefix)

    def journal(self, profile, since, prefix=""):
        return self.state_db.journal_since(profile, since, prefix)

    def commit(self, profile, host, run_id, lease_id, puts, dels, holds):
        return self.state_db.commit(profile, host, run_id, lease_id, puts, dels, holds,
                                    tree_root=self.root / profile / "tree",
                                    backup_dir=self.root / "backups" / run_id / profile)

    def holds(self, profile=None):
        return self.state_db.holds(profile)

    def put_kind_snapshot(self, kind, profile, rel, host, meta):
        return self.state_db.put_kind_snapshot(kind, profile, rel, host, meta)

    def kind_snapshots(self, kind, profile=None, rel=None):
        return self.state_db.kind_snapshots(kind, profile, rel)
