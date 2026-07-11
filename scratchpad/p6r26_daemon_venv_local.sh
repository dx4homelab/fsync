#!/usr/bin/env bash
# P6 R26 tail: move fsyncd off the dev checkout venv onto a standalone
# wheel-installed venv (~/.fsync/daemon-venv). Local (minis) half; fury is
# driven over ssh with the same wheelhouse.
set -euo pipefail

REPO=~/workspaces/homelab/fsync
WHEELHOUSE=~/.fsync/deploy/wheelhouse
VENV=~/.fsync/daemon-venv

echo "== 1/5 wheelhouse (fsync wheel + all deps) =="
rm -rf "$WHEELHOUSE" && mkdir -p "$WHEELHOUSE"
"$REPO/.venv/bin/pip" wheel "$REPO" -w "$WHEELHOUSE" --quiet
ls "$WHEELHOUSE" | sed 's/^/   /'

echo "== 2/5 standalone venv =="
rm -rf "$VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --no-index --find-links "$WHEELHOUSE" fsync

echo "== 3/5 smoke: version + daemon import chain =="
"$VENV/bin/python3" -m fsync.cli meta version
"$VENV/bin/python3" -c "import fsync.daemon; print('daemon module OK')"

echo "== 4/5 reinstall unit from the new venv + restart =="
"$VENV/bin/python3" -m fsync.cli daemon install
systemctl --user restart fsync-daemon.service
sleep 2

echo "== 5/5 verify =="
systemctl --user show -p ExecStart --value fsync-daemon.service
systemctl --user is-active fsync-daemon.service
journalctl --user -u fsync-daemon.service -n 5 --no-pager | grep -E 'listener|Started'
ss -tln | grep -E ':(7444|7446) ' || { echo "LISTENERS MISSING"; exit 1; }
echo "minis daemon on standalone venv: OK"
