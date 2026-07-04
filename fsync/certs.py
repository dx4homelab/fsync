"""Self-signed TLS material for fsyncd (P4.1, docs/home-sync-automation.md R10).

Trust model — deliberately NOT a CA hierarchy: each box generates ONE
self-signed keypair used for both server and client roles (dual EKU), and
peers pin each other's exact certificate, the same way the boxes already pin
ssh host keys. A self-signed cert is its own issuer, so loading the peer's
cert as a "CA" makes OpenSSL accept exactly that cert and nothing else.

Generation shells out to the openssl CLI: it is guaranteed present on these
boxes and avoids adding the `cryptography` wheel as a dependency.

Layout under ~/.config/fsync/tls/:
    server.key / server.pem   this box's identity (0600 key)
    trust/<name>.pem          pinned peer certificates
    trust-bundle.pem          concatenation of trust/*.pem (mTLS ca file)
"""

from __future__ import annotations

import secrets
import socket
import subprocess
from pathlib import Path

TLS_DIR = "~/.config/fsync/tls"
CERT_DAYS = 3650


class CertError(RuntimeError):
    pass


def tls_dir() -> Path:
    return Path(TLS_DIR).expanduser()


def key_path() -> Path:
    return tls_dir() / "server.key"


def cert_path() -> Path:
    return tls_dir() / "server.pem"


def trust_dir() -> Path:
    return tls_dir() / "trust"


def bundle_path() -> Path:
    return tls_dir() / "trust-bundle.pem"


def _san_list(hostname: str, extra: list[str] | None = None) -> list[str]:
    sans = [
        f"DNS:{hostname}",
        f"DNS:{hostname}.lan",
        f"DNS:{hostname}.local",
        "DNS:localhost",
        "IP:127.0.0.1",
    ]
    for e in extra or []:
        if e and e not in [s.split(":", 1)[1] for s in sans]:
            sans.append(("IP:" if e.replace(".", "").isdigit() else "DNS:") + e)
    return sans


def ensure_cert(extra_sans: list[str] | None = None) -> tuple[Path, Path]:
    """Create this box's keypair if absent; returns (cert, key) paths."""
    tls_dir().mkdir(parents=True, exist_ok=True)
    trust_dir().mkdir(parents=True, exist_ok=True)
    if cert_path().exists() and key_path().exists():
        return cert_path(), key_path()
    hostname = socket.gethostname().split(".")[0]
    san = ",".join(_san_list(hostname, extra_sans))
    proc = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-keyout", str(key_path()), "-out", str(cert_path()),
            "-days", str(CERT_DAYS), "-nodes",
            "-subj", f"/CN=fsyncd-{hostname}",
            "-addext", f"subjectAltName={san}",
            # one cert plays both roles: server to inbound UIs/peers, client
            # identity when this box calls the peer's mTLS listener
            "-addext", "extendedKeyUsage=serverAuth,clientAuth",
            # CA:FALSE = exact-cert pin: a self-signed leaf validates only as
            # ITSELF when used as its own trust anchor (verified: openssl
            # accepts it). CA:TRUE would let the key mint forged leaves that
            # also pass, defeating fingerprint-based revocation.
            "-addext", "basicConstraints=critical,CA:FALSE",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise CertError(f"openssl cert generation failed: {proc.stderr.strip()[-300:]}")
    key_path().chmod(0o600)
    return cert_path(), key_path()


def trust_peer(name: str, pem: str) -> Path:
    """Pin a peer certificate under trust/<name>.pem and rebuild the bundle."""
    if "BEGIN CERTIFICATE" not in pem:
        raise CertError(f"{name}: not a PEM certificate")
    trust_dir().mkdir(parents=True, exist_ok=True)
    dest = trust_dir() / f"{name}.pem"
    dest.write_text(pem)
    rebuild_bundle()
    return dest


def rebuild_bundle() -> Path | None:
    """Concatenate pinned certs into the mTLS ca file; None when no peers."""
    certs = sorted(trust_dir().glob("*.pem"))
    if not certs:
        bundle_path().unlink(missing_ok=True)
        return None
    bundle_path().write_text("\n".join(p.read_text().strip() for p in certs) + "\n")
    return bundle_path()


def trusted_peers() -> list[str]:
    return sorted(p.stem for p in trust_dir().glob("*.pem"))


def token_path() -> Path:
    return tls_dir() / "api-token"


def ensure_token() -> str:
    """Local-caller secret for the loopback listener (0600).

    The loopback TLS listener authenticates the server to the client but not
    the caller — every local account can reach 127.0.0.1. This token, readable
    only by our own user, gates local API access; mTLS peers are authenticated
    by their pinned client cert instead and never see this value.
    """
    p = token_path()
    if p.exists():
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    p.write_text(tok + "\n")
    p.chmod(0o600)
    return tok


def read_token() -> str | None:
    try:
        return token_path().read_text().strip()
    except OSError:
        return None


def cert_fingerprint(path: Path) -> str:
    proc = subprocess.run(
        ["openssl", "x509", "-in", str(path), "-noout", "-fingerprint", "-sha256"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise CertError(f"fingerprint failed for {path}")
    return proc.stdout.strip().split("=", 1)[-1]
