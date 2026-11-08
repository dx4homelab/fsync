"""Spoke-side client for the hub's control plane (P7.1, docs/hub-sync.md R37/R40).

Deliberately stdlib only — ``urllib`` + ``ssl`` — so it ships inside the
deployed .pyz (the laptops run no httpx). Trust is the exact-cert pin from
``certs.py``: the hub's self-signed certificate is the only trust anchor and
hostname checking is off, so the same pinned cert is accepted on the LAN name
and on the Tailscale name alike (R38). The spoke presents its own cert as the
mTLS client identity; the hub pins it in ``tls/trust/``.

Addresses are tried in order and the first one that answers is remembered,
so a dead LAN name costs one timeout per run, not one per request.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .hubstate import HubControl, LeaseHeld, LeaseLost

DEFAULT_TIMEOUT = 20.0
PROBE_TIMEOUT = 8.0


class HubUnavailable(RuntimeError):
    """No hub address answered."""


class HubApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


def _ssl_context(pinned_cert: str | None, identity: tuple[str, str] | None) -> ssl.SSLContext | None:
    if not pinned_cert:
        return None
    ctx = ssl.create_default_context(cafile=pinned_cert)
    ctx.check_hostname = False  # exact-cert pin is the identity proof (see certs.py)
    if identity:
        ctx.load_cert_chain(certfile=identity[0], keyfile=identity[1])
    return ctx


class HubClient:
    def __init__(self, base_urls: list[str], *, pinned_cert: str | None = None,
                 identity: tuple[str, str] | None = None, token: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT):
        if not base_urls:
            raise ValueError("HubClient needs at least one base URL")
        self.base_urls = [u.rstrip("/") for u in base_urls]
        self.active: str | None = None
        self.token = token
        self.timeout = timeout
        self._ctx = _ssl_context(pinned_cert, identity)

    # ------------------------------------------------------------------ http

    def _one(self, base: str, method: str, path: str, body: dict | None,
             params: dict | None, timeout: float) -> Any:
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            detail = raw.decode(errors="replace")
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict) and "detail" in payload:
                    detail = payload["detail"] if isinstance(payload["detail"], str) \
                        else json.dumps(payload["detail"])
            except ValueError:
                pass
            raise HubApiError(e.code, detail) from None

    def _request(self, method: str, path: str, body: dict | None = None,
                 params: dict | None = None) -> Any:
        """Try the remembered address, else each address in order; a transport
        failure moves on, an HTTP error is final (the hub answered)."""
        order = ([self.active] if self.active else []) + \
                [u for u in self.base_urls if u != self.active]
        last: Exception | None = None
        for base in order:
            timeout = self.timeout if base == self.active else PROBE_TIMEOUT
            try:
                out = self._one(base, method, path, body, params, timeout)
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = e
                if base == self.active:
                    self.active = None
                continue
            self.active = base
            return out
        raise HubUnavailable(f"hub not reachable at any of {self.base_urls}: {last}")

    # ------------------------------------------------------------------- api

    def status(self) -> dict:
        return self._request("GET", "/v1/hub/status")

    def lease_acquire(self, profile: str, host: str, run_id: str | None, ttl: int) -> dict:
        try:
            return self._request("POST", f"/v1/hub/{urllib.parse.quote(profile, safe='')}/lease",
                                 {"host": host, "run_id": run_id, "ttl": ttl})
        except HubApiError as e:
            if e.status == 409:
                raise LeaseHeld(profile, *_held_fields(e.detail)) from None
            raise

    def lease_renew(self, profile: str, lease_id: str, ttl: int) -> dict:
        try:
            return self._request("POST", f"/v1/hub/{urllib.parse.quote(profile, safe='')}/lease",
                                 {"lease_id": lease_id, "ttl": ttl})
        except HubApiError as e:
            if e.status == 410:
                raise LeaseLost(e.detail) from None
            raise

    def lease_release(self, profile: str, lease_id: str) -> bool:
        out = self._request("DELETE", f"/v1/hub/{urllib.parse.quote(profile, safe='')}"
                                      f"/lease/{urllib.parse.quote(lease_id, safe='')}")
        return bool((out or {}).get("released"))

    def record_run(self, host: str, profile: str, run_id: str, summary: dict) -> None:
        self._request("POST", f"/v1/hub/{urllib.parse.quote(profile, safe='')}/runs",
                      {"host": host, "run_id": run_id, "summary": summary})

    # P7.2
    def state(self, profile: str, prefix: str = "") -> dict:
        return self._request("GET", f"/v1/hub/{urllib.parse.quote(profile, safe='')}/state",
                             params={"prefix": prefix or None})

    def journal(self, profile: str, since: int, prefix: str = "") -> dict:
        return self._request("GET", f"/v1/hub/{urllib.parse.quote(profile, safe='')}/journal",
                             params={"since": since, "prefix": prefix or None})

    def commit(self, profile: str, host: str, run_id: str, lease_id: str | None,
               puts: list[dict], dels: list[dict], holds: list[dict]) -> dict:
        try:
            return self._request("POST", f"/v1/hub/{urllib.parse.quote(profile, safe='')}/commit",
                                 {"host": host, "run_id": run_id, "lease_id": lease_id,
                                  "puts": puts, "dels": dels, "holds": holds})
        except HubApiError as e:
            if e.status == 410:
                raise LeaseLost(e.detail) from None
            raise

    def holds(self, profile: str | None = None) -> list[dict]:
        path = (f"/v1/hub/{urllib.parse.quote(profile, safe='')}/holds" if profile
                else "/v1/hub/holds")
        return self._request("GET", path) or []


def _held_fields(detail: str) -> tuple[str, str | None, float]:
    """The 409 body is JSON {holder, run_id, expires} when the hub produced it;
    fall back to a readable holder string otherwise."""
    try:
        d = json.loads(detail)
        return str(d.get("holder", "?")), d.get("run_id"), float(d.get("expires", 0))
    except (ValueError, TypeError, AttributeError):
        return detail or "another spoke", None, 0.0


class RemoteHubControl(HubControl):
    def __init__(self, client: HubClient):
        self.client = client

    def acquire(self, profile, host, run_id, ttl):
        return self.client.lease_acquire(profile, host, run_id, ttl)

    def renew(self, profile, lease_id, ttl):
        return self.client.lease_renew(profile, lease_id, ttl)

    def release(self, profile, lease_id):
        return self.client.lease_release(profile, lease_id)

    def record_run(self, host, profile, run_id, summary):
        self.client.record_run(host, profile, run_id, summary)

    def status(self):
        return self.client.status()

    def state(self, profile, prefix=""):
        return self.client.state(profile, prefix)

    def journal(self, profile, since, prefix=""):
        return self.client.journal(profile, since, prefix)

    def commit(self, profile, host, run_id, lease_id, puts, dels, holds):
        return self.client.commit(profile, host, run_id, lease_id, puts, dels, holds)

    def holds(self, profile=None):
        return self.client.holds(profile)
