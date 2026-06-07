"""HTTP event emission + consumption for fsync, backed by ``dream4devops.events``.

This lets scanners report status/progress and receive commands over the
dream4events HTTP API instead of connecting to Postgres directly. Everything
here is optional: with no events URL configured, :class:`FsyncEvents` is a
no-op, so ``fsync index`` keeps working standalone.

dream4events ships only a *producer* SDK (``EventClient``) and has no SSE, so
the *receive* side is :class:`EventConsumer` — a polling consumer with a
persisted ULID cursor. Delivery is at-least-once; handlers must be idempotent.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SOURCE = "fsync"

# Event type taxonomy (dot-namespaced "<domain>.<verb>").
EV_SCANNER_REGISTERED = "scanner.registered"
EV_SCANNER_HEARTBEAT = "scanner.heartbeat"
EV_SCAN_STARTED = "scan.started"
EV_SCAN_PROGRESS = "scan.progress"
EV_SCAN_FILE_PROGRESS = "scan.file.progress"
EV_SCAN_COMPLETED = "scan.completed"
EV_SCAN_FAILED = "scan.failed"
EV_COMMAND = "scanner.command"
EV_COMMAND_ACK = "scanner.command.ack"
EV_COMMAND_RESULT = "scanner.command.result"


class FsyncEvents:
    """Producer wrapper around the dream4events ``EventClient``.

    A no-op when ``base_url`` is falsy. Emission failures are swallowed (logged)
    so telemetry never breaks a scan. ``instance`` is always the scanner id and
    ``source`` is ``"fsync"``; ``correlation_id`` groups events of one run/command.
    """

    def __init__(self, base_url: Optional[str], scanner_id: str, *, logger: Any | None = None, source: str = SOURCE):
        self.scanner_id = scanner_id
        self.logger = logger
        self._source = source
        self._client = None
        self.enabled = bool(base_url)
        if self.enabled:
            from dream4devops.events import EventClient

            self._client = EventClient(base_url, source=source)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def __enter__(self) -> "FsyncEvents":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def emit(
        self,
        type: str,
        body: Dict[str, Any],
        *,
        correlation_id: Optional[str] = None,
        timestamp: Optional[int] = None,
        idempotency_parts: Optional[list] = None,
    ) -> Optional[str]:
        """Emit one event. Returns the event id, or None when disabled/failed.

        ``idempotency_parts`` -> a deterministic ULID via ``ulid_from`` so a
        re-emit of the same logical event is a server-side no-op.
        """
        if not self.enabled:
            return None
        ts = int(timestamp if timestamp is not None else time.time())
        ev_id = None
        if idempotency_parts:
            from dream4devops.events import ulid_from

            ev_id = ulid_from(ts, *[str(p) for p in idempotency_parts])
        try:
            return self._client.emit(
                type=type,
                timestamp=ts,
                instance=self.scanner_id,
                correlation_id=correlation_id,
                body=body,
                id=ev_id,
            )
        except Exception as e:  # never let telemetry break the caller
            if self.logger:
                self.logger.warning("event emit failed (%s): %s", type, e)
            return None

    # --- convenience emitters ---------------------------------------------
    def registered(self, **info: Any) -> Optional[str]:
        return self.emit(EV_SCANNER_REGISTERED, {"scanner_id": self.scanner_id, **info})

    def heartbeat(self, status: str, **extra: Any) -> Optional[str]:
        return self.emit(EV_SCANNER_HEARTBEAT, {"scanner_id": self.scanner_id, "status": status, **extra})

    def scan_started(self, run_id: str, source_label: str, **extra: Any) -> Optional[str]:
        return self.emit(EV_SCAN_STARTED, {"scanner_id": self.scanner_id, "source": source_label, **extra}, correlation_id=run_id)

    def scan_progress(self, run_id: str, files_done: int, bytes_done: Optional[int] = None, **extra: Any) -> Optional[str]:
        body: Dict[str, Any] = {"files_done": files_done}
        if bytes_done is not None:
            body["bytes_done"] = bytes_done
        body.update(extra)
        return self.emit(EV_SCAN_PROGRESS, body, correlation_id=run_id)

    def scan_file_progress(self, run_id: str, path: str, size: int, bytes_hashed: int) -> Optional[str]:
        pct = round(100.0 * bytes_hashed / size, 1) if size else 100.0
        return self.emit(
            EV_SCAN_FILE_PROGRESS,
            {"path": path, "size": size, "bytes_hashed": bytes_hashed, "pct": pct},
            correlation_id=run_id,
        )

    def scan_completed(self, run_id: str, **stats: Any) -> Optional[str]:
        return self.emit(EV_SCAN_COMPLETED, {"scanner_id": self.scanner_id, **stats}, correlation_id=run_id)

    def scan_failed(self, run_id: str, error: Any) -> Optional[str]:
        return self.emit(EV_SCAN_FAILED, {"scanner_id": self.scanner_id, "error": str(error)[:500]}, correlation_id=run_id)

    def command_ack(self, command_id: str, **extra: Any) -> Optional[str]:
        return self.emit(EV_COMMAND_ACK, {"scanner_id": self.scanner_id, "command_id": command_id, **extra},
                         correlation_id=command_id, idempotency_parts=["ack", self.scanner_id, command_id])

    def command_result(self, command_id: str, state: str, **extra: Any) -> Optional[str]:
        return self.emit(EV_COMMAND_RESULT, {"scanner_id": self.scanner_id, "command_id": command_id, "state": state, **extra},
                         correlation_id=command_id, idempotency_parts=["result", self.scanner_id, command_id, state])


class EventConsumer:
    """Polling consumer over ``GET /events`` with a persisted ULID cursor.

    Returns events strictly after the cursor, oldest-first. ``timestamp`` is
    only epoch-seconds, so the cursor is the ``(timestamp, id)`` pair and ULID
    ids break ties within a second. Delivery is at-least-once — persist the
    cursor and make handlers idempotent.
    """

    def __init__(self, base_url: str, *, cursor_path: Optional[str] = None, logger: Any | None = None, http: Any | None = None, start_ts: Optional[int] = None):
        self.base_url = base_url.rstrip("/")
        self.logger = logger
        self.cursor_path = Path(cursor_path) if cursor_path else None
        self._last_ts = 0
        self._last_id = ""
        loaded = False
        if self.cursor_path and self.cursor_path.is_file():
            try:
                data = json.loads(self.cursor_path.read_text())
                self._last_ts = int(data.get("last_ts", 0))
                self._last_id = str(data.get("last_id", ""))
                loaded = True
            except Exception:
                pass
        if not loaded and start_ts is not None:
            # Fresh consumer: skip history, only see events from start_ts on.
            self._last_ts = int(start_ts)
        import httpx

        self._http = http or httpx.Client(timeout=15.0)

    def _save_cursor(self) -> None:
        if not self.cursor_path:
            return
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cursor_path.with_suffix(self.cursor_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"last_ts": self._last_ts, "last_id": self._last_id}))
        tmp.replace(self.cursor_path)

    def poll(self, *, type: Optional[str] = None, instance: Optional[str] = None,
             correlation_id: Optional[str] = None, page_size: int = 200, max_pages: int = 500) -> List[Dict[str, Any]]:
        """Fetch new events since the cursor (ascending), advancing the cursor."""
        base_params: Dict[str, Any] = {"limit": page_size, "since": self._last_ts}
        if type:
            base_params["type"] = type
        if instance:
            base_params["instance"] = instance
        if correlation_id:
            base_params["correlation_id"] = correlation_id
        collected: List[Dict[str, Any]] = []
        for page in range(max_pages):
            params = dict(base_params, offset=page * page_size)
            r = self._http.get(f"{self.base_url}/events", params=params)
            r.raise_for_status()
            events = r.json().get("events", [])
            collected.extend(events)
            if len(events) < page_size:
                break
        cursor = (self._last_ts, self._last_id)
        fresh = [e for e in collected if (e["timestamp"], e["id"]) > cursor]
        fresh.sort(key=lambda e: (e["timestamp"], e["id"]))
        if fresh:
            self._last_ts = fresh[-1]["timestamp"]
            self._last_id = fresh[-1]["id"]
            self._save_cursor()
        return fresh

    def close(self) -> None:
        self._http.close()
