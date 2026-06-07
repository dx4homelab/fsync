"""HTTP client for the fsync catalog API.

Lets a scanner push catalog batches over HTTP (``POST /catalog/batch``) instead
of opening a Postgres connection. Mirrors the store_index batch contract.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class CatalogClient:
    def __init__(self, base_url: str, source: str, *, timeout: float = 60.0, http: Any | None = None, logger: Any | None = None):
        self.base_url = base_url.rstrip("/")
        self.source = source
        self.logger = logger
        import httpx

        self._http = http or httpx.Client(timeout=timeout)

    def post_batch(self, records: List[Dict[str, Any]]) -> int:
        if not records:
            return 0
        r = self._http.post(
            f"{self.base_url}/catalog/batch",
            json={"source": self.source, "records": records},
        )
        r.raise_for_status()
        return int(r.json().get("stored", 0))

    def stats(self) -> Dict[str, Any]:
        r = self._http.get(f"{self.base_url}/catalog/stats", params={"source": self.source})
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "CatalogClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
