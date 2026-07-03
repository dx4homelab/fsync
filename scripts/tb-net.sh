#!/usr/bin/env bash
#
# tb-net.sh — set up Thunderbolt/USB4 host-to-host networking for fsync.
#
# Connect two Linux boxes with a TB4/USB4 cable and each side gets a
# point-to-point ~10-20 Gbit/s ethernet link (kernel thunderbolt_net /
# XDomain). Run this on BOTH machines with a different last octet, then
# point `fsync sync-plan` / rsync at the peer's 10.55.0.x address.
#
# What it does (idempotent):
#   - modprobe thunderbolt_net, and persist it via /etc/modules-load.d/
#   - add a NetworkManager profile "tb-p2p" bound to interface
#     thunderbolt0 with a static /30 address and MTU 65520
#
# The thunderbolt0 interface only exists while a cable connects the two
# hosts; the profile auto-activates on hotplug. A direct machine-to-machine
# cable is the reliable setup — going through a TB4 hub/dock port usually
# works too, but try direct first if the link doesn't appear.
#
# Usage:
#   sudo ./tb-net.sh 1      # first machine  -> 10.55.0.1/30
#   sudo ./tb-net.sh 2      # second machine -> 10.55.0.2/30
#
# Verify (after plugging the cable, from machine 1):
#   ip -br addr show thunderbolt0
#   ping 10.55.0.2
#   ping -M do -s 65492 10.55.0.2   # confirms jumbo MTU end to end
#
# If the profile refuses to activate on an older kernel (<5.13 caps the
# tbnet MTU below 64k), drop the jumbo MTU:
#   nmcli connection modify tb-p2p 802-3-ethernet.mtu 0

set -euo pipefail

SUBNET_PREFIX="10.55.0"
CON_NAME="tb-p2p"
IFNAME="thunderbolt0"
MTU=65520

if [[ $EUID -ne 0 ]]; then
    echo "error: run as root (sudo $0 ...)" >&2
    exit 1
fi

if [[ $# -ne 1 || ! "$1" =~ ^[12]$ ]]; then
    echo "usage: sudo $0 <1|2>   (1 -> ${SUBNET_PREFIX}.1/30, 2 -> ${SUBNET_PREFIX}.2/30)" >&2
    exit 1
fi

ADDR="${SUBNET_PREFIX}.$1/30"

modprobe thunderbolt_net
echo thunderbolt_net > /etc/modules-load.d/thunderbolt_net.conf
echo "loaded thunderbolt_net (persisted in /etc/modules-load.d/)"

if nmcli -t -f NAME connection show | grep -qx "$CON_NAME"; then
    nmcli connection modify "$CON_NAME" \
        ipv4.method manual ipv4.addresses "$ADDR" 802-3-ethernet.mtu "$MTU"
    echo "updated NetworkManager profile '$CON_NAME' -> $ADDR"
else
    nmcli connection add type ethernet ifname "$IFNAME" con-name "$CON_NAME" \
        ipv4.method manual ipv4.addresses "$ADDR" \
        ipv6.method link-local \
        802-3-ethernet.mtu "$MTU" \
        connection.autoconnect yes
    echo "created NetworkManager profile '$CON_NAME' -> $ADDR"
fi

echo "done. Plug a TB4/USB4 cable between the two machines and check:"
echo "  ip -br addr show $IFNAME"
