"""FastAPI catalog service for fsync.

Fronts the ``file_index`` table with HTTP so scanners POST catalog batches and
clients query, instead of connecting to Postgres directly. This is the
"catalog plane" complement to the dream4events "telemetry/control plane".

Run:
    python -m fsync.catalog_api --dsn postgresql://user:pass@host:5432/db --port 8081
or set DB_URL in the environment.
"""
from __future__ import annotations

import argparse
import os
from contextlib import contextmanager
from typing import Any, List, Optional

from pydantic import BaseModel


class CatalogBatch(BaseModel):
    # Defined at module scope so FastAPI resolves the annotation as a request
    # body (a model nested inside create_app gets mis-read as a query param).
    source: str
    records: List[dict]


def create_app(dsn: str):
    from fastapi import FastAPI, HTTPException, Query
    import psycopg2
    import psycopg2.extras

    from .db import store_index

    app = FastAPI(title="fsync-catalog", version="0.1.0")

    @contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/health")
    def health() -> dict:
        try:
            with connect() as c, c.cursor() as cur:
                cur.execute("select 1")
            return {"ok": True}
        except Exception:
            raise HTTPException(status_code=503, detail="db unreachable")

    @app.post("/catalog/batch")
    def post_batch(batch: CatalogBatch) -> dict:
        """Upsert a batch of file records for one source. Idempotent on (source, path)."""
        if not batch.records:
            return {"stored": 0}
        try:
            store_index(dsn, batch.records, source=batch.source)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"store failed: {e}")
        return {"stored": len(batch.records)}

    @app.get("/catalog/stats")
    def stats(source: str) -> dict:
        with connect() as c, c.cursor() as cur:
            cur.execute(
                """
                select count(*),
                       count(distinct metadata->>'hash'),
                       coalesce(sum((metadata->>'size')::bigint), 0)
                from file_index where source = %s
                """,
                (source,),
            )
            files, unique_hashes, logical = cur.fetchone()
            cur.execute(
                """
                select coalesce(sum(sz), 0) from (
                  select max((metadata->>'size')::bigint) sz
                  from file_index where source = %s group by metadata->>'hash'
                ) t
                """,
                (source,),
            )
            unique_bytes = cur.fetchone()[0]
        return {
            "source": source,
            "files": int(files),
            "unique_hashes": int(unique_hashes),
            "logical_bytes": int(logical),
            "unique_bytes": int(unique_bytes),
        }

    @app.get("/catalog/sources")
    def sources() -> dict:
        with connect() as c, c.cursor() as cur:
            cur.execute("select source, count(*) from file_index group by source order by 2 desc")
            rows = cur.fetchall()
        return {"sources": [{"source": s, "files": int(n)} for s, n in rows]}

    @app.get("/catalog")
    def query(
        source: Optional[str] = None,
        hash: Optional[str] = None,
        path_prefix: Optional[str] = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict:
        clauses: List[str] = []
        params: List[Any] = []
        if source:
            clauses.append("source = %s")
            params.append(source)
        if hash:
            clauses.append("metadata->>'hash' = %s")
            params.append(hash)
        if path_prefix:
            clauses.append("path like %s")
            params.append(path_prefix + "%")
        where = ("where " + " and ".join(clauses)) if clauses else ""
        with connect() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"select source, path, metadata from file_index {where} order by id limit %s offset %s",
                params + [limit, offset],
            )
            rows = cur.fetchall()
        return {"rows": rows, "limit": limit, "offset": offset}

    return app


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="fsync.catalog_api")
    p.add_argument("--dsn", default=os.environ.get("DB_URL"), help="Postgres DSN (or set DB_URL)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8081)
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("--dsn or DB_URL required")
    import uvicorn

    uvicorn.run(create_app(args.dsn), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
