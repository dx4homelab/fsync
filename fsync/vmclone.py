"""``fsync vm clone`` — clone a libvirt/virt-manager VM from a REMOTE host.

Pulls the domain definition + every file disk + the UEFI NVRAM varstore over
SSH, regenerates identity (UUID + MACs), re-paths storage into a local pool,
and defines the clone locally (left shut off). Remote qemu-owned images are read
via ``sudo rsync``; local privileged writes go through ``sudo``; disks are
relabeled ``virt_image_t`` for SELinux.

The definition surgery (:func:`rewrite_domain_xml`) is pure and unit-tested; the
rest orchestrates virsh / rsync / qemu-img. This is the tested, config-aware
sibling of ``scratchpad/virt-clone-remote.sh``.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


class VmCloneError(Exception):
    """A clone step failed in a way the user must resolve."""


# --------------------------------------------------------------------------- #
# pure, unit-testable: the clone definition rewrite                           #
# --------------------------------------------------------------------------- #
def rewrite_domain_xml(src_xml: str, *, name: str, disk_map: dict[str, str],
                       nvram_new: str | None = None,
                       network: str | None = None) -> str:
    """Return a cloned domain XML derived from ``src_xml``.

    - ``<name>`` set to ``name``
    - ``<uuid>`` removed  → libvirt regenerates a fresh one on define
    - every interface ``<mac>`` removed → fresh MACs (a copied MAC would clash
      with the source on a shared bridge)
    - disk ``<source file=...>`` re-pathed via ``disk_map`` (old → new)
    - UEFI ``<os><nvram>`` re-pathed to ``nvram_new`` when given
    - optional NIC remap via ``network``: ``"default"`` / ``"network:NAME"``
      (type=network) or ``"bridge:NAME"`` (type=bridge)
    """
    root = ET.fromstring(src_xml)

    name_el = root.find("name")
    if name_el is None:
        raise VmCloneError("source XML has no <name> element")
    name_el.text = name

    uuid_el = root.find("uuid")
    if uuid_el is not None:
        root.remove(uuid_el)

    for iface in root.findall("./devices/interface"):
        mac = iface.find("mac")
        if mac is not None:
            iface.remove(mac)
        if network:
            src = iface.find("source")
            if src is None:
                src = ET.SubElement(iface, "source")
            for k in list(src.attrib):
                del src.attrib[k]
            if network.startswith("bridge:"):
                iface.set("type", "bridge")
                src.set("bridge", network.split(":", 1)[1])
            else:
                net = network.split(":", 1)[1] if network.startswith("network:") else network
                iface.set("type", "network")
                src.set("network", net)

    for src_el in root.findall("./devices/disk/source"):
        f = src_el.get("file")
        if f is not None and f in disk_map:
            src_el.set("file", disk_map[f])

    nvram_el = root.find("./os/nvram")
    if nvram_el is not None and nvram_new:
        nvram_el.text = nvram_new

    return ET.tostring(root, encoding="unicode")


def disk_sources(domain_xml: str) -> list[str]:
    """File-backed disk source paths, in document order (device='disk' only)."""
    root = ET.fromstring(domain_xml)
    out: list[str] = []
    for disk in root.findall("./devices/disk"):
        if disk.get("device", "disk") != "disk":
            continue
        src = disk.find("source")
        if src is not None and src.get("file"):
            out.append(src.get("file"))
    return out


def nvram_source(domain_xml: str) -> str | None:
    """The UEFI NVRAM varstore path, or None for a BIOS/non-pflash guest."""
    nv = ET.fromstring(domain_xml).find("./os/nvram")
    return nv.text.strip() if (nv is not None and nv.text) else None


# --------------------------------------------------------------------------- #
# orchestration                                                               #
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], *, check: bool = True, capture: bool = True,
         **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check,
                          capture_output=capture, text=True, **kw)


def _pool_dir(connect: str, pool: str) -> str | None:
    """Target path of a libvirt storage pool, or None if it doesn't exist."""
    p = _run(["virsh", "-c", connect, "pool-dumpxml", pool], check=False)
    if p.returncode != 0:
        return None
    root = ET.fromstring(p.stdout)
    tp = root.find("./target/path")
    return tp.text if tp is not None else None


def _default_target_dir(connect: str, pool: str | None) -> str:
    """Resolve where clone disks land: explicit pool → its path; else the roomy
    ``vmstore`` pool if defined; else the stock ``images`` pool; else the
    libvirt default images dir."""
    for candidate in (pool, "vmstore", "images"):
        if not candidate:
            continue
        d = _pool_dir(connect, candidate)
        if d:
            return d
    return "/var/lib/libvirt/images"


def clone_vm(args: argparse.Namespace, log) -> int:
    src_host: str = args.src_host
    src_user: str = args.user
    src_vm: str = args.vm
    name: str = args.name or f"{src_vm}-clone"
    connect: str = args.connect
    nvram_dir: str = args.nvram_dir
    network: str | None = args.network
    compress: bool = args.compress

    ruri = f"qemu+ssh://{src_user}@{src_host}/system"
    lv = ["virsh", "-c", connect]
    rv = ["virsh", "-c", ruri]
    target_dir = args.target_dir or _default_target_dir(connect, args.pool)

    log(f"source : {src_user}@{src_host} :: {src_vm}")
    log(f"target : {name} -> {connect}  disk-dir={target_dir}")

    # --- preflight ---------------------------------------------------------
    st = _run([*rv, "domstate", src_vm], check=False)
    if st.returncode != 0:
        raise VmCloneError(f"remote VM '{src_vm}' not found on {src_host}: {st.stderr.strip()}")
    state = st.stdout.strip()
    log(f"remote state: {state}")
    if state == "running" and not args.force:
        raise VmCloneError("source is RUNNING — its disk is in flux; shut it off or pass --force")
    if _run([*lv, "dominfo", name], check=False).returncode == 0:
        raise VmCloneError(f"a local domain named '{name}' already exists — pick another --name")

    xml = _run([*rv, "dumpxml", "--inactive", src_vm]).stdout
    disks = disk_sources(xml)
    if not disks:
        raise VmCloneError("source has no file-backed disks")
    src_nvram = nvram_source(xml)

    disk_map: dict[str, str] = {}
    new_disks: list[str] = []
    for i, d in enumerate(disks):
        base = f"{name}.qcow2" if len(disks) == 1 else f"{name}-{i}.qcow2"
        new = os.path.join(target_dir, base)
        if os.path.exists(new):
            raise VmCloneError(f"target disk already exists: {new}")
        disk_map[d] = new
        new_disks.append(new)
    new_nvram = os.path.join(nvram_dir, f"{name}_VARS.qcow2") if src_nvram else None

    log(f"disks  : {', '.join(disks)}")
    log(f"  ->    : {', '.join(new_disks)}")
    if src_nvram:
        log(f"nvram  : {src_nvram} -> {new_nvram}")
    if network:
        log(f"network: remap -> {network}")
    if compress:
        log("disk   : qcow2 compression ON")

    if args.dry_run:
        log("dry-run — no transfer, no define.")
        return 0

    stage = Path(args.stage_dir or tempfile.mkdtemp(prefix=".virt-clone-stage.",
                                                    dir=os.path.expanduser("~")))
    stage.mkdir(parents=True, exist_ok=True)
    log(f"stage  : {stage}")
    try:
        # --- transfer (remote reads via sudo; sparse + resumable) ----------
        staged: list[Path] = []
        for i, d in enumerate(disks):
            sp = stage / f"disk-{i}.qcow2"
            log(f"pulling {os.path.basename(d)} ...")
            _rsync(src_user, src_host, d, sp, sparse=True, verbose=args.verbose)
            staged.append(sp)
        staged_nvram = None
        if src_nvram:
            staged_nvram = stage / "nvram.qcow2"
            log("pulling nvram varstore ...")
            _rsync(src_user, src_host, src_nvram, staged_nvram, sparse=False, verbose=args.verbose)

        # --- place into libvirt storage (privileged), relabel, own ---------
        for sp, nd in zip(staged, new_disks):
            if compress:
                log(f"compress+install {os.path.basename(nd)} ...")
                _run(["sudo", "qemu-img", "convert", "-O", "qcow2", "-c", str(sp), nd], capture=False)
            else:
                log(f"install {os.path.basename(nd)} ...")
                _run(["sudo", "cp", "--sparse=always", str(sp), nd], capture=False)
            _own_and_label(nd)
        if new_nvram and staged_nvram:
            _run(["sudo", "cp", "--sparse=always", str(staged_nvram), new_nvram], capture=False)
            _own_and_label(new_nvram)

        # --- rewrite + define ---------------------------------------------
        clone_xml = rewrite_domain_xml(xml, name=name, disk_map=disk_map,
                                       nvram_new=new_nvram, network=network)
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as fh:
            fh.write(clone_xml)
            xml_path = fh.name
        try:
            _run([*lv, "define", xml_path], capture=False)
        finally:
            os.unlink(xml_path)
        log(f"defined clone '{name}'")

        # --- verify --------------------------------------------------------
        for nd in new_disks:
            chk = _run(["sudo", "qemu-img", "check", nd], check=False)
            log(f"  check {os.path.basename(nd)}: {(chk.stdout or chk.stderr).strip().splitlines()[-1]}")
        _run([*lv, "dominfo", name], check=False, capture=False)

        if args.boot_test:
            log(f"boot test: starting '{name}' ...")
            _run([*lv, "start", name], capture=False)
            import time
            time.sleep(20)
            bstate = _run([*lv, "domstate", name]).stdout.strip()
            _run([*lv, "destroy", name], check=False)
            log(f"boot test: state after 20s = {bstate}")
            if bstate != "running":
                raise VmCloneError("clone did not stay running during boot test")
            log("boot test PASSED (booted, then forced off)")
    finally:
        import shutil
        shutil.rmtree(stage, ignore_errors=True)

    log(f"DONE — clone '{name}' is defined (left shut off).")
    return 0


def _rsync(user: str, host: str, remote_path: str, local_path: Path, *,
           sparse: bool, verbose: int) -> None:
    cmd = ["rsync", "-a", "--rsync-path=sudo rsync", "-e", "ssh -o BatchMode=yes"]
    if sparse:
        cmd.append("--sparse")
    cmd += ["--info=progress2"] if verbose else []
    cmd += [f"{user}@{host}:{remote_path}", str(local_path)]
    _run(cmd, capture=False)


def _own_and_label(path: str) -> None:
    _run(["sudo", "chown", "qemu:qemu", path], capture=False)
    if _run(["sudo", "restorecon", "-F", path], check=False).returncode != 0:
        _run(["sudo", "chcon", "-t", "virt_image_t", path], check=False)


def _vm_config_defaults(config: str | None) -> dict[str, Any]:
    """Optional ``vm:`` mapping from the profiles YAML — defaults for user /
    pool / network / nvram_dir. Read directly (not via load_config) so it never
    interferes with sync-profile validation, and is absent-safe."""
    path = Path(config) if config else Path("~/.config/fsync/sync-profiles.yaml").expanduser()
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}
    vm = data.get("vm")
    return vm if isinstance(vm, dict) else {}


def cmd_vm(args: argparse.Namespace) -> int:
    """`fsync vm clone` dispatcher."""
    def log(msg: str) -> None:
        print(f"[vm] {msg}", file=sys.stderr)

    if getattr(args, "vm_cmd", None) != "clone":
        print("usage: fsync vm clone --from HOST --vm NAME [--name CLONE] [...]", file=sys.stderr)
        return 2

    # config-driven defaults fill only what the CLI left unset
    defaults = _vm_config_defaults(getattr(args, "config", None))
    if args.user is None:
        args.user = defaults.get("user", os.environ.get("USER", "root"))
    if args.pool is None:
        args.pool = defaults.get("pool")
    if args.network is None:
        args.network = defaults.get("network")
    if args.nvram_dir is None:
        args.nvram_dir = defaults.get("nvram_dir", "/var/lib/libvirt/qemu/nvram")

    try:
        return clone_vm(args, log)
    except VmCloneError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
