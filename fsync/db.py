"""Database helpers for storing file index entries into PostgreSQL.

This module imports psycopg2 only when needed so the rest of the package
doesn't require the DB client for normal usage.
"""
from __future__ import annotations

from typing import Iterable, Dict, Any, Optional
from urllib.parse import urlparse


def store_index(db_url: str, items: Iterable[Dict[str, Any]], table: str = "file_index") -> None:
    """Store a list of metadata dictionaries into Postgres table `table`.

    The table is expected to have at least columns: path TEXT PRIMARY KEY, metadata JSONB.
    The function performs INSERT ... ON CONFLICT DO UPDATE to upsert rows.
    """
    try:
        import json
        import psycopg2
        import psycopg2.extras
    except Exception as exc:  # pragma: no cover - psycopg2 may not be installed in test env
        raise RuntimeError("psycopg2 is required to store index to DB; install psycopg2-binary") from exc

    # Normalize db_url
    if not db_url:
        raise ValueError("db_url is required")

    conn = psycopg2.connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                # We will upsert by path; convert metadata dict to JSON
                sql = f"INSERT INTO {table} (path, metadata) VALUES (%s, %s) ON CONFLICT (path) DO UPDATE SET metadata = EXCLUDED.metadata"
                records = []
                for it in items:
                    path = it.get("path") or it.get("name")
                    metadata = dict(it)
                    # Remove path/name from metadata to avoid duplication
                    metadata.pop("path", None)
                    metadata.pop("name", None)
                    records.append((path, psycopg2.extras.Json(metadata)))

                if records:
                    psycopg2.extras.execute_batch(cur, sql, records)
    finally:
        conn.close()
