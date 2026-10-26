#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
# Install the Nature AI Worker Platform Client (S-C) as a separate Linux
# installation: its own unprivileged system account, its own data root and
# its own process. The existing Standalone installation is not touched.
#
#   sudo scripts/platform-client-linux-install.sh \
#        --origin https://<platform>.ts.net \
#        --standalone-data /home/<user>/nature-ai-worker/data
#
# Remove with scripts/platform-client-linux-uninstall.sh.
#
# Layout (shared with nature-worker-upgrade, see docs/UPGRADES.md): each build
# is a complete venv in $PREFIX/versions/<version>+g<commit>/, `current` names
# the active one, and the service runs $PREFIX/bin/nature-worker-platform-client.
# Builds made here are UNSIGNED local builds, never reported as official
# releases. Signed releases are installed with:
#   sudo $PREFIX/bin/nature-worker-upgrade upgrade --bundle DIR
set -euo pipefail

ACCOUNT=nature-pc
PREFIX=/opt/nature-worker-platform-client
DATA=/var/lib/nature-worker-platform-client
ENV_FILE=/etc/nature-worker-platform-client.env
UNIT=/etc/systemd/system/nature-worker-platform-client.service
PORT=8090
ORIGIN="" STANDALONE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --origin) ORIGIN="$2"; shift 2 ;;
    --standalone-data) STANDALONE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 2; }
case "$ORIGIN" in https://*) ;; *) echo "--origin must be an https:// Platform origin" >&2; exit 2 ;; esac
[ -n "$STANDALONE" ] || { echo "--standalone-data is required (path of the Standalone data root)" >&2; exit 2; }
[ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] || \
  { echo "run with sudo from the account that owns the checkout" >&2; exit 2; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
cd /  # never import from, or build in, the user's checkout
STANDALONE_UID="$(stat -c %u "$STANDALONE")" || { echo "cannot read $STANDALONE" >&2; exit 2; }

if [ -e "$DATA" ] && [ -n "$(ls -A "$DATA" 2>/dev/null)" ] && \
   ! grep -q '"platform_client"' "$DATA/installation-mode.json" 2>/dev/null; then
  echo "$DATA exists and is not a Platform Client installation; refusing" >&2; exit 1
fi

# Stop a running instance before replacing its code: a process must never keep
# serving old code beside new files. It is started again only after preflight.
# The port must be free unless this service is the one using it. (On WSL every
# distribution shares one network: another distribution may hold the port.)
OURS=""
if systemctl is-active --quiet nature-worker-platform-client.service && \
   grep -qx "NWPC_PORT=$PORT" "$ENV_FILE" 2>/dev/null; then OURS=1; fi
if [ -z "$OURS" ] && \
   python3 -I -c 'import socket, sys; s = socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)' "$PORT"; then
  echo "127.0.0.1:$PORT is already used by another program; choose another --port" >&2; exit 1
fi
systemctl stop nature-worker-platform-client.service 2>/dev/null || true

id -u "$ACCOUNT" >/dev/null 2>&1 || \
  useradd --system --user-group --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$ACCOUNT"

# Install exactly the committed HEAD, exported by the checkout's owner, into a
# root-owned staging copy: nothing is built in (or written to) the checkout.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
COMMIT="$(runuser -u "$SUDO_USER" -- git -C "$SRC" rev-parse HEAD)"
runuser -u "$SUDO_USER" -- git -C "$SRC" diff --quiet HEAD -- || \
  echo "note: uncommitted changes in $SRC are NOT installed; installing $COMMIT"
runuser -u "$SUDO_USER" -- git -C "$SRC" archive --format=tar HEAD > "$STAGE/src.tar"
mkdir "$STAGE/src" && tar -xf "$STAGE/src.tar" -C "$STAGE/src"

# Code is root-owned and read-only for the service account. Dependencies are
# installed from wheels only, so no third-party build code runs as root. Each
# build gets its own directory; the active one is replaced only by switching
# `current`, so code is never changed under a running process.
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$STAGE/src/src/nature_worker/__init__.py")"
LABEL="$VERSION+g${COMMIT:0:12}"
VDIR="$PREFIX/versions/$LABEL"
install -d -o root -g root -m 0755 "$PREFIX" "$PREFIX/versions"
if [ ! -f "$VDIR/INSTALLED.json" ] || [ -e "$VDIR/.incomplete" ]; then
  rm -rf "$VDIR"
  install -d -o root -g root -m 0755 "$VDIR"
  touch "$VDIR/.incomplete"
  python3 -m venv "$VDIR/venv"
  "$VDIR/venv/bin/pip" install -q --only-binary=:all: -r "$STAGE/src/requirements.lock"
  "$VDIR/venv/bin/pip" install -q --no-deps "$STAGE/src"
fi
"$VDIR/venv/bin/python" -I -m nature_worker.upgrade register-local-build --prefix "$PREFIX" \
  --version "$LABEL" --edition platform_client --data-dir "$DATA" --account "$ACCOUNT" \
  --service nature-worker-platform-client.service --port "$PORT" --commit "$COMMIT"
echo "$COMMIT" > "$PREFIX/COMMIT"
echo "installed commit $COMMIT as $LABEL"

install -d -o "$ACCOUNT" -g "$ACCOUNT" -m 0700 "$DATA"
runuser -u "$ACCOUNT" -- env NWPC_DATA_DIR="$DATA" "$VDIR/venv/bin/python" -I -m nature_worker.platform_client init

umask 027
cat > "$ENV_FILE" <<EOF
NWPC_DATA_DIR=$DATA
NWPC_PLATFORM_ORIGIN=$ORIGIN
NWPC_STANDALONE_DATA_DIR=$STANDALONE
NWPC_STANDALONE_UID=$STANDALONE_UID
NWPC_HOST=127.0.0.1
NWPC_PORT=$PORT
EOF
chown root:"$ACCOUNT" "$ENV_FILE"; chmod 0640 "$ENV_FILE"

umask 022
cat > "$UNIT" <<EOF
[Unit]
Description=Nature AI Worker Platform Client (S-C)
After=network-online.target
Wants=network-online.target

[Service]
User=$ACCOUNT
Group=$ACCOUNT
EnvironmentFile=$ENV_FILE
WorkingDirectory=$DATA
ExecStart=$PREFIX/bin/nature-worker-platform-client serve
Restart=on-failure
UMask=0077
LimitCORE=0
NoNewPrivileges=yes
CapabilityBoundingSet=
RestrictSUIDSGID=yes
LockPersonality=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$DATA
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
echo "--- preflight (as $ACCOUNT)"
if runuser -u "$ACCOUNT" -- env $(grep -v '^#' "$ENV_FILE" | xargs) \
     "$VDIR/venv/bin/python" -I -m nature_worker.platform_client preflight; then
  systemctl enable nature-worker-platform-client.service
  systemctl restart nature-worker-platform-client.service
  # "active" only means systemd started it: wait until THIS service answers on its port.
  if ! "$VDIR/venv/bin/python" -I - "$PORT" "$ORIGIN" <<'PY'
import json, sys, time, urllib.request
port, origin = sys.argv[1], sys.argv[2]
for _ in range(60):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
            if json.load(r).get("platform") == origin:
                sys.exit(0)
    except (OSError, ValueError):
        pass
    time.sleep(1)
sys.exit(1)
PY
  then
    echo "the service did not answer on 127.0.0.1:$PORT; see: journalctl -u nature-worker-platform-client" >&2
    exit 1
  fi
  # The layout before versioned installs kept one venv in $PREFIX/venv: no longer used.
  if [ -d "$PREFIX/venv" ]; then rm -rf "$PREFIX/venv"; fi
  # Keep the active build and the two most recent others (for rollback); remove older local builds.
  { ls -1t "$PREFIX/versions" | grep -vxF "$LABEL" | grep -F '+g' | tail -n +3 | \
    while read -r old; do rm -rf "${PREFIX:?}/versions/$old"; done; } || true
  echo "Platform Client running $LABEL: open http://127.0.0.1:$PORT/ and sign in again"
else
  echo "Preflight blocked: the service was NOT started. Resolve the findings above." >&2
  exit 1
fi
