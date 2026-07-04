"""API client for fsyncd — the ONLY module UIs may use to reach the engine
(P4.1, R9). Pure HTTP over TLS: no engine imports, no state-file knowledge.

Local use: FsyncClient() -> https://127.0.0.1:7444, verified against this
box's own self-signed server cert (never verify=False).

Peer use (mTLS): FsyncClient.for_peer("minis4dx") -> verified against the
pinned peer cert, presenting this box's cert as client identity.
"""

from __future__ import annotations

import json
import ssl

import httpx

from . import certs

DEFAULT_PORT = 7444
USER_TIMEOUT = httpx.Timeout(10.0, read=60.0)


def _ssl_context(pinned_cert: str, identity: tuple[str, str] | None) -> ssl.SSLContext:
    """TLS context pinned to exactly ``pinned_cert`` (the peer/server's
    self-signed cert), optionally presenting ``identity`` as a client cert
    for mTLS. httpx 0.28's tuple ``cert=`` no longer presents a client cert
    when combined with a custom verify target, so the cert must be loaded
    into an explicit context instead."""
    ctx = ssl.create_default_context(cafile=pinned_cert)
    # SANs cover host/.lan/.local/localhost/127.0.0.1, so hostname checking
    # stays on — the pin already constrains us to the one exact cert.
    if identity:
        ctx.load_cert_chain(certfile=identity[0], keyfile=identity[1])
    return ctx


class DaemonUnavailable(RuntimeError):
    """fsyncd is not reachable (not installed / not running)."""


class ApiError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class FsyncClient:
    def __init__(self, base_url: str | None = None, port: int = DEFAULT_PORT,
                 verify: Any = None, identity: tuple[str, str] | None = None,
                 timeout: httpx.Timeout = USER_TIMEOUT, token: str | None = None,
                 send_token: bool = True):
        if verify is None:
            cert = certs.cert_path()
            if not cert.exists():
                raise DaemonUnavailable(
                    "no TLS material at ~/.config/fsync/tls — run `fsync daemon install`")
            verify = str(cert)
        # A path string means "pin this cert"; build an explicit SSLContext so
        # the client cert (mTLS) is actually presented. A caller may also pass
        # a ready SSLContext/bool for tests.
        ssl_target = _ssl_context(verify, identity) if isinstance(verify, str) else verify
        self.base_url = base_url or f"https://127.0.0.1:{port}"
        # loopback endpoints are token-gated; a local caller can read the 0600
        # token file (same user). Peer/mTLS clients pass send_token=False —
        # their pinned client cert is the credential and they can't read ours.
        headers = {}
        if send_token:
            tok = token or certs.read_token()
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
        self._http = httpx.Client(base_url=self.base_url, verify=ssl_target,
                                  timeout=timeout, headers=headers)

    @classmethod
    def for_peer(cls, name: str, host: str | None = None,
                 port: int = DEFAULT_PORT) -> "FsyncClient":
        """mTLS client for a pinned peer: their cert as trust anchor, our
        cert+key as client identity."""
        pinned = certs.trust_dir() / f"{name}.pem"
        if not pinned.exists():
            raise DaemonUnavailable(f"peer '{name}' is not trusted — run `fsync daemon trust`")
        return cls(base_url=f"https://{host or name}:{port}",
                   verify=str(pinned),
                   identity=(str(certs.cert_path()), str(certs.key_path())),
                   send_token=False)  # authenticated by client cert, not token

    # ------------------------------------------------------------------ core

    def _req(self, method: str, path: str, body: dict | None = None,
             params: dict | None = None) -> Any:
        try:
            resp = self._http.request(method, path, json=body, params=params)
        except httpx.TransportError as e:
            raise DaemonUnavailable(f"fsyncd not reachable at {self.base_url}: {e}") from e
        if resp.status_code >= 400:
            detail = resp.text
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    detail = payload.get("detail", resp.text)
            except (ValueError, json.JSONDecodeError):
                pass
            raise ApiError(resp.status_code, str(detail))
        return resp.json()

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ api

    def status(self) -> dict:
        return self._req("GET", "/v1/status")

    def profiles(self) -> dict:
        return self._req("GET", "/v1/profiles")

    def plan_start(self, profiles: list[str] | None = None,
                   workers: int | None = None) -> str:
        body: dict = {}
        if profiles:
            body["profiles"] = profiles
        if workers:
            body["workers"] = workers
        return self._req("POST", "/v1/plan", body)["job_id"]

    def plan_get(self, job_id: str) -> dict:
        return self._req("GET", f"/v1/plan/{job_id}")

    def run_start(self, profiles: list[str] | None = None,
                  dry_run: bool = False) -> dict:
        body: dict = {"dry_run": dry_run}
        if profiles:
            body["profiles"] = profiles
        return self._req("POST", "/v1/runs", body)

    def run_current(self) -> dict | None:
        try:
            return self._req("GET", "/v1/runs/current")
        except ApiError as e:
            if e.status_code == 404:
                return None
            raise

    def runs(self, limit: int = 20) -> list[dict]:
        return self._req("GET", "/v1/runs", params={"limit": limit})

    def run_report(self, run_id: str) -> dict:
        return self._req("GET", f"/v1/runs/{run_id}/report")

    def conflicts(self) -> dict:
        return self._req("GET", "/v1/conflicts")

    def peer(self) -> dict:
        return self._req("GET", "/v1/peer")

    def schedule(self) -> dict:
        return self._req("GET", "/v1/schedule")

    def schedule_set(self, enabled: bool | None = None,
                     interval: str | None = None) -> dict:
        body: dict = {}
        if enabled is not None:
            body["enabled"] = enabled
        if interval is not None:
            body["interval"] = interval
        return self._req("PUT", "/v1/schedule", body)
