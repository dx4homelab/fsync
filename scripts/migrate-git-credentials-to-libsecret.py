#!/usr/bin/env python3
"""Move git credentials from the plain-text `store` helper into the libsecret keyring.

The `store` helper keeps every token in clear text in ~/.git-credentials (or the
file named by `credential.helper store --file=...`). This script re-stores each
entry through `git credential-libsecret` (GNOME Keyring / any Secret Service),
reads it back to prove the keyring has it, then — with --apply — switches
`credential.helper` to `libsecret` in your global git config and retires the
plain file. It never prints a password.

Typical use (run on EACH box that has the plain file):

    migrate-git-credentials-to-libsecret.py            # dry run: shows what it would do
    migrate-git-credentials-to-libsecret.py --apply    # migrate, verify, switch helper, delete the plain file
    migrate-git-credentials-to-libsecret.py --apply --retire backup   # ...keep a 0600 plain-text copy instead

The plain file is deleted (not backed up) by default on purpose: a backup is
still clear text, and on a home directory that is synced between machines
(fsync `dotfiles`) it would be replicated. If your home is synced, also exclude
`.git-credentials*` from that sync before running this, and run it on every
box — otherwise the peer's copy comes straight back.

Preconditions checked up front: git-credential-libsecret is installed, a
Secret Service answers on the session bus, and its default collection is
unlocked (over ssh that means a logged-in desktop session, or unlock it first).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

HELPER = "libsecret"


@dataclass
class Cred:
    protocol: str
    host: str
    username: str | None
    password: str
    path: str | None
    source: str      # file:line, for messages

    @property
    def label(self) -> str:
        who = f"{self.username}@" if self.username else ""
        return f"{self.protocol}://{who}{self.host}{'/' + self.path if self.path else ''}"

    def to_helper_input(self, with_password: bool) -> str:
        lines = [f"protocol={self.protocol}", f"host={self.host}"]
        if self.username is not None:
            lines.append(f"username={self.username}")
        if self.path:
            lines.append(f"path={self.path}")
        if with_password:
            lines.append(f"password={self.password}")
        return "\n".join(lines) + "\n\n"


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #

def git(*args: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          input=input_text, check=check, timeout=60)


def configured_helpers() -> list[str]:
    r = git("config", "--global", "--get-all", "credential.helper", check=False)
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def store_files_from_config(helpers: list[str]) -> list[Path]:
    """Files named by `store --file=X` / `store --file X` helper entries."""
    out: list[Path] = []
    for h in helpers:
        parts = h.split()
        if not parts or parts[0] != "store":
            continue
        for i, tok in enumerate(parts[1:], 1):
            if tok.startswith("--file="):
                out.append(Path(tok[len("--file="):]).expanduser())
            elif tok == "--file" and i + 1 < len(parts):
                out.append(Path(parts[i + 1]).expanduser())
    return out


def default_store_files() -> list[Path]:
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return [Path.home() / ".git-credentials", xdg / "git" / "credentials"]


def parse_store(path: Path) -> list[Cred]:
    """One `protocol://[user[:pass]@]host[/path]` per line, percent-encoded."""
    creds: list[Cred] = []
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            u = urlsplit(line)
        except ValueError:
            print(f"  ! {path}:{n}: unparseable line, skipped", file=sys.stderr)
            continue
        if not u.scheme or not u.hostname:
            print(f"  ! {path}:{n}: no protocol/host, skipped", file=sys.stderr)
            continue
        if u.password is None:
            print(f"  ! {path}:{n}: no password in entry, skipped", file=sys.stderr)
            continue
        host = u.hostname
        if u.port:
            host = f"{host}:{u.port}"
        creds.append(Cred(
            protocol=u.scheme,
            host=host,
            username=unquote(u.username) if u.username is not None else None,
            password=unquote(u.password),
            path=u.path.lstrip("/") or None,
            source=f"{path}:{n}",
        ))
    return creds


# --------------------------------------------------------------------------- #
# preconditions
# --------------------------------------------------------------------------- #

def check_preconditions() -> list[str]:
    problems: list[str] = []
    exec_path = git("--exec-path").stdout.strip()
    if not (Path(exec_path) / f"git-credential-{HELPER}").exists():
        problems.append(f"git-credential-{HELPER} is not installed (looked in {exec_path}); "
                        f"on Fedora/Bluefin: rpm-ostree install git-credential-libsecret")
    if shutil.which("busctl"):
        r = subprocess.run(["busctl", "--user", "call", "org.freedesktop.secrets",
                            "/org/freedesktop/secrets", "org.freedesktop.Secret.Service",
                            "ReadAlias", "s", "default"], capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or '"/org/freedesktop/secrets/collection/' not in r.stdout:
            problems.append("no Secret Service on the session bus (is a keyring daemon running "
                            "in this session? over ssh, the desktop session must be logged in)")
        else:
            coll = r.stdout.split('"')[1]
            if coll.endswith("/collection/"):
                problems.append("Secret Service has no default collection (create/unlock the "
                                "login keyring first)")
            else:
                lk = subprocess.run(["busctl", "--user", "get-property", "org.freedesktop.secrets",
                                     coll, "org.freedesktop.Secret.Collection", "Locked"],
                                    capture_output=True, text=True, timeout=15)
                if lk.returncode == 0 and lk.stdout.strip().endswith("true"):
                    problems.append(f"default keyring collection {coll} is LOCKED — unlock it "
                                    f"(log in to the desktop, or: secret-tool lookup x y) and retry")
    else:
        problems.append("busctl not found; cannot verify the Secret Service — proceeding blind "
                        "is not safe, install systemd tools or run from a desktop session")
    return problems


# --------------------------------------------------------------------------- #
# migration
# --------------------------------------------------------------------------- #

def keyring_get(c: Cred) -> str | None:
    """Password the libsecret helper returns for this protocol/host/username, if any."""
    r = git(f"credential-{HELPER}", "get", check=False, input_text=c.to_helper_input(False))
    if r.returncode != 0:
        return None
    for ln in r.stdout.splitlines():
        if ln.startswith("password="):
            return ln[len("password="):]
    return None


def keyring_store(c: Cred) -> None:
    r = git(f"credential-{HELPER}", "store", check=False, input_text=c.to_helper_input(True))
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git credential-{HELPER} store rc={r.returncode}")


def migrate(creds: list[Cred], apply: bool) -> tuple[int, int, int]:
    """Returns (stored, already_present, failed)."""
    stored = present = failed = 0
    for c in creds:
        have = keyring_get(c)
        if have == c.password:
            print(f"  = {c.label}: already in keyring")
            present += 1
            continue
        if not apply:
            print(f"  + {c.label}: would store" + (" (keyring has a DIFFERENT password; would replace)" if have else ""))
            continue
        try:
            keyring_store(c)
        except RuntimeError as e:
            print(f"  ! {c.label}: store failed: {e}", file=sys.stderr)
            failed += 1
            continue
        back = keyring_get(c)
        if back == c.password:
            print(f"  + {c.label}: stored and read back OK" + (" (replaced)" if have else ""))
            stored += 1
        else:
            print(f"  ! {c.label}: stored but read-back {'differs' if back else 'is empty'} — NOT trusting it",
                  file=sys.stderr)
            failed += 1
    return stored, present, failed


def switch_helper(helpers: list[str], apply: bool) -> None:
    keep = [h for h in helpers if h.split()[:1] != ["store"]]
    dropped = [h for h in helpers if h.split()[:1] == ["store"]]
    if HELPER not in keep:
        keep.append(HELPER)
    if keep == helpers:
        print(f"  = credential.helper already {keep}")
        return
    print(f"  {'->' if apply else 'would set'} global credential.helper = {keep}"
          + (f"  (dropping {dropped})" if dropped else ""))
    if not apply:
        return
    git("config", "--global", "--unset-all", "credential.helper", check=False)
    for h in keep:
        git("config", "--global", "--add", "credential.helper", h)


def retire(path: Path, mode: str, apply: bool) -> None:
    if mode == "keep":
        print(f"  = {path}: kept (plain text still on disk — delete it once pushes work)")
        return
    if mode == "delete":
        print(f"  {'->' if apply else 'would'} delete {path}")
        if apply:
            path.unlink()
        return
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(path.name + f".migrated-{stamp}")
    print(f"  {'->' if apply else 'would'} move {path} -> {bak} (mode 0600; still plain text — "
          f"delete it once pushes work)")
    if apply:
        os.replace(path, bak)
        os.chmod(bak, 0o600)


# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", action="append", type=Path,
                    help="plain store file to migrate (repeatable; default: the ones git would use)")
    ap.add_argument("--apply", action="store_true", help="do it (default is a dry run)")
    ap.add_argument("--retire", choices=["delete", "backup", "keep"], default="delete",
                    help="what to do with the plain file after a fully verified migration (default: delete)")
    ap.add_argument("--no-set-helper", action="store_true",
                    help="migrate the entries but leave credential.helper alone")
    args = ap.parse_args(argv)

    print("preconditions:")
    problems = check_preconditions()
    for p in problems:
        print(f"  ! {p}", file=sys.stderr)
    if problems:
        return 2
    print(f"  ok: git-credential-{HELPER} present, Secret Service up, default collection unlocked")

    helpers = configured_helpers()
    print(f"global credential.helper: {helpers or '(none)'}")
    files = args.file or [f for f in (store_files_from_config(helpers) + default_store_files()) if f.is_file()]
    files = list(dict.fromkeys(f.expanduser().resolve() for f in files))   # dedupe, keep order
    if not files:
        print("nothing to migrate: no plain store file found")
        switch_helper(helpers, args.apply and not args.no_set_helper)
        return 0

    total_failed = 0
    for f in files:
        creds = parse_store(f)
        print(f"{f}: {len(creds)} entr{'y' if len(creds) == 1 else 'ies'}")
        stored, present, failed = migrate(creds, args.apply)
        total_failed += failed
        if args.apply and failed:
            print(f"  ! {failed} entr{'y' if failed == 1 else 'ies'} NOT verified in the keyring — "
                  f"leaving {f} in place", file=sys.stderr)
        else:
            retire(f, args.retire, args.apply)

    if total_failed:
        print("helper NOT switched: fix the failures above and re-run", file=sys.stderr)
        return 1
    if not args.no_set_helper:
        switch_helper(helpers, args.apply)
    if not args.apply:
        print("\ndry run — nothing changed. Re-run with --apply to migrate.")
    else:
        print("\ndone. Test with e.g.:  printf 'protocol=https\\nhost=github.com\\n\\n' | git credential fill")
    return 0


if __name__ == "__main__":
    sys.exit(main())
