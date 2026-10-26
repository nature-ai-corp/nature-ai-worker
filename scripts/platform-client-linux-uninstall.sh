#!/usr/bin/env bash
# Remove the Linux Platform Client installation. Local S-C data is deleted only
# with --delete-local-data, after the manifest below is shown. Platform server
# data is never touched. The Standalone installation is not touched.
#
#   sudo scripts/platform-client-linux-uninstall.sh [--delete-local-data]
set -euo pipefail

ACCOUNT=nature-pc
PREFIX=/opt/nature-worker-platform-client
DATA=/var/lib/nature-worker-platform-client
ENV_FILE=/etc/nature-worker-platform-client.env
UNIT=/etc/systemd/system/nature-worker-platform-client.service
DELETE=0
[ "${1:-}" = "--delete-local-data" ] && DELETE=1
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 2; }

# Stopping the service ends the process and discards its in-memory session.
systemctl disable --now nature-worker-platform-client.service 2>/dev/null || true
rm -f "$UNIT" "$ENV_FILE"
systemctl daemon-reload
rm -rf "$PREFIX"

echo "Local Platform Client data manifest:"
find "$DATA" -xdev -printf '  %M %u %p\n' 2>/dev/null || echo "  (none)"
if [ "$DELETE" -eq 1 ]; then
  rm -rf --one-file-system "$DATA"
  userdel "$ACCOUNT" 2>/dev/null || true
  getent group "$ACCOUNT" >/dev/null && groupdel "$ACCOUNT" || true
  echo "Local Platform Client data and account removed."
else
  echo "Data kept. Re-run with --delete-local-data to remove it and the $ACCOUNT account."
fi
pgrep -u "$ACCOUNT" >/dev/null 2>&1 && { echo "A $ACCOUNT process is still running" >&2; exit 1; }
exit 0
