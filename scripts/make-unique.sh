#!/usr/bin/env bash
#
# make-unique.sh — give a freshly disk-cloned Linux box its own identity.
#
# Run this ON THE CLONE after imaging a source disk. It regenerates every
# per-machine identifier that would otherwise collide with the source:
#   - /etc/machine-id        (drives DHCP client-id/DUID -> "same IP" bug)
#   - hostname
#   - SSH host keys          (clones present identical keys -> insecure)
#   - systemd random seed    (shared entropy on first boot)
#   - cached DHCP leases
#
# It deliberately does NOT touch filesystem/LVM/btrfs UUIDs — that only
# matters if both disks are attached to one machine, and changing the root
# UUID can make the box unbootable. Handle those separately if needed.
#
# Usage:
#   sudo ./make-unique.sh [options] <new-hostname>
#
# Options:
#   --expect-id <id>   Abort unless current /etc/machine-id == <id>.
#                      Use the SOURCE machine's id here: the script only
#                      runs on a not-yet-uniquified clone, and refuses to
#                      run twice (after step 1 the id no longer matches).
#   --reboot           Reboot when finished (recommended).
#   --dry-run          Print actions, change nothing.
#   -y, --yes          Skip the confirmation prompt.
#   -h, --help         Show this help.
#
# Exit codes: 0 ok, 1 usage/precondition error, 2 guard mismatch.

set -euo pipefail

# ---- defaults ---------------------------------------------------------------
EXPECT_ID=""
DO_REBOOT=0
DRY_RUN=0
ASSUME_YES=0
NEW_HOSTNAME=""

PROG="${0##*/}"

die()  { printf '%s: error: %s\n' "$PROG" "$*" >&2; exit 1; }
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }

usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; s/^#//'; exit "${1:-0}"; }

# run a command, or just echo it under --dry-run
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '   [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# ---- parse args -------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --expect-id) EXPECT_ID="${2:-}"; [[ -n "$EXPECT_ID" ]] || die "--expect-id needs a value"; shift 2 ;;
    --expect-id=*) EXPECT_ID="${1#*=}"; shift ;;
    --reboot)  DO_REBOOT=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -y|--yes)  ASSUME_YES=1; shift ;;
    -h|--help) usage 0 ;;
    --) shift; break ;;
    -*) die "unknown option: $1 (try --help)" ;;
    *)  [[ -z "$NEW_HOSTNAME" ]] || die "unexpected extra argument: $1"; NEW_HOSTNAME="$1"; shift ;;
  esac
done
[[ $# -gt 0 && -z "$NEW_HOSTNAME" ]] && { NEW_HOSTNAME="$1"; shift; }

# ---- preconditions ----------------------------------------------------------
[[ -n "$NEW_HOSTNAME" ]] || die "missing <new-hostname> (try --help)"

# RFC 1123 hostname label: lowercase letters, digits, hyphens; 1-63 chars;
# no leading/trailing hyphen. (Single label — no dots.)
if ! [[ "$NEW_HOSTNAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
  die "invalid hostname '$NEW_HOSTNAME' (use a-z 0-9 '-', 1-63 chars, no leading/trailing '-')"
fi

if [[ $DRY_RUN -eq 0 && $EUID -ne 0 ]]; then
  die "must run as root — re-run with: sudo $PROG $*"
fi

CURRENT_ID="$(cat /etc/machine-id 2>/dev/null || true)"
CURRENT_HOST="$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo '?')"

# Clone guard: only run on a box whose id still matches the source image,
# which also makes a second run a no-op (the id changes in step 1).
if [[ -n "$EXPECT_ID" ]]; then
  if [[ "$CURRENT_ID" != "$EXPECT_ID" ]]; then
    printf '%s: guard mismatch — current machine-id (%s) != --expect-id (%s)\n' \
      "$PROG" "${CURRENT_ID:-<empty>}" "$EXPECT_ID" >&2
    printf '       This box has already been uniquified (or is not the expected clone). Refusing.\n' >&2
    exit 2
  fi
  ok "guard passed: machine-id matches expected source id"
fi

# ---- confirmation -----------------------------------------------------------
cat <<EOF
About to make this machine unique:
  hostname     : $CURRENT_HOST  ->  $NEW_HOSTNAME
  machine-id   : $CURRENT_ID  ->  (new random)
  ssh host keys: regenerate
  random-seed  : remove (regenerated on boot)
  dhcp leases  : clear
  reboot after : $([[ $DO_REBOOT -eq 1 ]] && echo yes || echo no)
  mode         : $([[ $DRY_RUN -eq 1 ]] && echo DRY-RUN || echo APPLY)
EOF

if [[ $ASSUME_YES -eq 0 && $DRY_RUN -eq 0 ]]; then
  read -r -p "Proceed? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || die "aborted by user"
fi

# ---- 1. machine-id ----------------------------------------------------------
info "Regenerating /etc/machine-id"
run rm -f /etc/machine-id
run systemd-machine-id-setup
# Keep dbus's id in sync. Fedora ships it as a symlink; recreate if it's a
# stale regular file copied from the source image.
if [[ -L /var/lib/dbus/machine-id ]]; then
  ok "/var/lib/dbus/machine-id is a symlink (follows /etc/machine-id)"
else
  info "Relinking /var/lib/dbus/machine-id -> /etc/machine-id"
  run rm -f /var/lib/dbus/machine-id
  run ln -s /etc/machine-id /var/lib/dbus/machine-id
fi

# ---- 2. hostname ------------------------------------------------------------
info "Setting hostname -> $NEW_HOSTNAME"
run hostnamectl set-hostname "$NEW_HOSTNAME"
# Keep an /etc/hosts 127.0.1.1 entry consistent if one exists for the old name.
if [[ $DRY_RUN -eq 0 ]] && grep -qE '^\s*127\.0\.1\.1\s' /etc/hosts 2>/dev/null; then
  run sed -i "s/^\(\s*127\.0\.1\.1\s\+\).*/\1$NEW_HOSTNAME/" /etc/hosts
  ok "updated 127.0.1.1 line in /etc/hosts"
fi

# ---- 3. SSH host keys -------------------------------------------------------
if compgen -G '/etc/ssh/ssh_host_*' >/dev/null; then
  info "Regenerating SSH host keys"
  run bash -c 'rm -f /etc/ssh/ssh_host_*'
  run ssh-keygen -A
  if systemctl list-unit-files 2>/dev/null | grep -q '^sshd\.service'; then
    run systemctl try-restart sshd
  elif systemctl list-unit-files 2>/dev/null | grep -q '^ssh\.service'; then
    run systemctl try-restart ssh
  fi
else
  ok "no /etc/ssh host keys present — skipping"
fi

# ---- 4. systemd random seed -------------------------------------------------
if [[ -e /var/lib/systemd/random-seed ]]; then
  info "Removing cloned systemd random-seed"
  run rm -f /var/lib/systemd/random-seed
else
  ok "no random-seed present — skipping"
fi

# ---- 5. cached DHCP leases --------------------------------------------------
info "Clearing cached DHCP leases"
run bash -c 'rm -f /var/lib/NetworkManager/*.lease /var/lib/NetworkManager/*-lease* /var/lib/dhclient/*.leases 2>/dev/null || true'

# ---- done -------------------------------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then
  info "Dry run complete — nothing changed."
  exit 0
fi

NEW_ID="$(cat /etc/machine-id 2>/dev/null || echo '?')"
info "Done."
printf '   new hostname : %s\n   new machine-id: %s\n' "$NEW_HOSTNAME" "$NEW_ID"

if [[ $DO_REBOOT -eq 1 ]]; then
  info "Rebooting now…"
  run systemctl reboot
else
  printf '\n\033[1;33mReboot recommended\033[0m so the new machine-id/DHCP identity takes effect:\n  sudo reboot\n'
fi
