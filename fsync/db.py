"""Database helpers for storing file index entries into PostgreSQL.

This module imports psycopg2 only when needed so the rest of the package
doesn't require the DB client for normal usage.
"""
from __future__ import annotations

from typing import Iterable, Dict, Any, Optional
from urllib.parse import urlparse


def store_index(
    db_url: str,
    items: Iterable[Dict[str, Any]],
    source: str,
    table: str = "file_index",
) -> None:
    """Upsert a list of metadata dictionaries into Postgres table `table`.

    Rows are keyed by ``(source, path)`` so the same relative path coming from
    different roots/hosts does not collide — this is what lets a single table
    act as a central catalog across many sources. `source` is a caller-chosen
    label (e.g. an absolute path or ``host:path``).

    The table is created if it does not already exist, so this works against a
    fresh database as well as one initialised by docker/initdb.
    """
    try:
        import psycopg2
        import psycopg2.extras
    except Exception as exc:  # pragma: no cover - psycopg2 may not be installed in test env
        raise RuntimeError("psycopg2 is required to store index to DB; install psycopg2-binary") from exc

    if not db_url:
        raise ValueError("db_url is required")
    if not source:
        raise ValueError("source label is required")

    conn = psycopg2.connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id SERIAL PRIMARY KEY,
                        source TEXT NOT NULL,
                        path TEXT NOT NULL,
                        metadata JSONB NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT now(),
                        updated_at TIMESTAMPTZ DEFAULT now(),
                        UNIQUE (source, path)
                    )
                    """
                )
                sql = (
                    f"INSERT INTO {table} (source, path, metadata) VALUES (%s, %s, %s) "
                    "ON CONFLICT (source, path) DO UPDATE "
                    "SET metadata = EXCLUDED.metadata, updated_at = now()"
                )
                records = []
                for it in items:
                    path = it.get("path") or it.get("name")
                    metadata = dict(it)
                    # Remove path/name from metadata to avoid duplication
                    metadata.pop("path", None)
                    metadata.pop("name", None)
                    records.append((source, path, psycopg2.extras.Json(metadata)))

                if records:
                    psycopg2.extras.execute_batch(cur, sql, records)
    finally:
        conn.close()
