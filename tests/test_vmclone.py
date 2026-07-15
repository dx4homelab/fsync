"""`fsync vm clone` — unit tests for the pure definition-rewrite surgery.

The transfer/define steps need libvirt+ssh (covered by live use); here we pin the
identity regeneration and re-pathing that make a clone safe to run alongside its
source: fresh UUID, fresh MACs, re-pathed disks + UEFI nvram, optional NIC remap.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from fsync.vmclone import (
    disk_sources, nvram_source, rewrite_domain_xml, VmCloneError,
)

# A UEFI/SecureBoot guest much like the real fedora-41-wickr: pflash nvram, one
# qcow2 disk, a cdrom (must be ignored), and a bridged virtio NIC.
SRC = """<domain type='kvm'>
  <name>fedora-41-wickr</name>
  <uuid>893abf35-1925-452d-982d-d55513dce522</uuid>
  <memory unit='KiB'>16580608</memory>
  <os firmware='efi'>
    <type arch='x86_64' machine='pc-q35-9.1'>hvm</type>
    <loader readonly='yes' secure='yes' type='pflash'>/usr/share/edk2/ovmf/OVMF_CODE_4M.secboot.qcow2</loader>
    <nvram template='/usr/share/edk2/ovmf/OVMF_VARS_4M.secboot.qcow2'>/var/lib/libvirt/qemu/nvram/fedora-41-wickr_VARS.qcow2</nvram>
  </os>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/var/lib/libvirt/images/fedora-41-wickr.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <source file='/var/lib/libvirt/images/seed.iso'/>
      <target dev='sda' bus='sata'/>
    </disk>
    <interface type='bridge'>
      <mac address='52:54:00:0b:92:68'/>
      <source bridge='bridge0'/>
      <model type='virtio'/>
    </interface>
  </devices>
</domain>"""

DMAP = {"/var/lib/libvirt/images/fedora-41-wickr.qcow2":
        "/var/home/vm-images/wickr-clone.qcow2"}


def _clone(**kw):
    kw.setdefault("name", "wickr-clone")
    kw.setdefault("disk_map", DMAP)
    return ET.fromstring(rewrite_domain_xml(SRC, **kw))


def test_name_renamed():
    assert _clone().find("name").text == "wickr-clone"


def test_uuid_dropped_for_regen():
    assert _clone().find("uuid") is None          # libvirt mints a fresh one on define


def test_mac_dropped_for_regen():
    # a copied MAC would collide with the source on a shared L2 segment
    assert _clone().find("./devices/interface/mac") is None


def test_disk_source_repathed():
    src = _clone().find("./devices/disk[@device='disk']/source")
    assert src.get("file") == "/var/home/vm-images/wickr-clone.qcow2"


def test_cdrom_source_untouched():
    # only the mapped data disk is re-pathed; the cdrom is left as-is
    cdrom = _clone().findall("./devices/disk")[1]
    assert cdrom.get("device") == "cdrom"
    assert cdrom.find("source").get("file") == "/var/lib/libvirt/images/seed.iso"


def test_nvram_repathed():
    nv = _clone(nvram_new="/var/lib/libvirt/qemu/nvram/wickr-clone_VARS.qcow2").find("./os/nvram")
    assert nv.text == "/var/lib/libvirt/qemu/nvram/wickr-clone_VARS.qcow2"
    assert nv.get("template") == "/usr/share/edk2/ovmf/OVMF_VARS_4M.secboot.qcow2"  # template preserved


def test_network_remap_to_default():
    iface = _clone(network="default").find("./devices/interface")
    assert iface.get("type") == "network"
    assert iface.find("source").get("network") == "default"
    assert iface.find("source").get("bridge") is None       # old bridge attr cleared
    assert iface.find("mac") is None


def test_network_remap_to_bridge():
    iface = _clone(network="bridge:br0").find("./devices/interface")
    assert iface.get("type") == "bridge"
    assert iface.find("source").get("bridge") == "br0"


def test_network_preserved_when_unset():
    iface = _clone().find("./devices/interface")            # no network= → source untouched
    assert iface.get("type") == "bridge"
    assert iface.find("source").get("bridge") == "bridge0"


def test_disk_sources_lists_only_data_disks_in_order():
    assert disk_sources(SRC) == ["/var/lib/libvirt/images/fedora-41-wickr.qcow2"]


def test_nvram_source_detected():
    assert nvram_source(SRC) == "/var/lib/libvirt/qemu/nvram/fedora-41-wickr_VARS.qcow2"


def test_nvram_source_none_for_bios_guest():
    bios = "<domain><name>x</name><os><type>hvm</type></os><devices/></domain>"
    assert nvram_source(bios) is None


def test_missing_name_raises():
    with pytest.raises(VmCloneError):
        rewrite_domain_xml("<domain><os/><devices/></domain>", name="x", disk_map={})
