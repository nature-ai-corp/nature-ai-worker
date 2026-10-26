#!/usr/bin/env bash
# Install the Nature AI Worker Platform Client (S-C) as a separate Linux
# installation: its own unprivileged system account, its own data root and
# its own process. The existing Standalone installation is not touched.
#
#   sudo scripts/platform-client-linux-install.sh \
#        --origin https://<platform>.ts.net \
#        --standalone-data /home/<user>/nature-ai-worker/data
#
# Remove with scripts/platform-client-linux-uninstall.sh.
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
SRC="$(cd "$(dirname "$0")/.." && pwd)"
STANDALONE_UID="$(stat -c %u "$STANDALONE")" || { echo "cannot read $STANDALONE" >&2; exit 2; }

if [ -e "$DATA" ] && [ -n "$(ls -A "$DATA" 2>/dev/null)" ] && \
   ! grep -q '"platform_client"' "$DATA/installation-mode.json" 2>/dev/null; then
  echo "$DATA exists and is not a Platform Client installation; refusing" >&2; exit 1
fi

id -u "$ACCOUNT" >/dev/null 2>&1 || \
  useradd --system --user-group --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$ACCOUNT"

# Code is root-owned and read-only for the service account.
install -d -o root -g root -m 0755 "$PREFIX"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q -r "$SRC/requirements.lock"
"$PREFIX/venv/bin/pip" install -q --no-deps --force-reinstall "$SRC"

install -d -o "$ACCOUNT" -g "$ACCOUNT" -m 0700 "$DATA"
runuser -u "$ACCOUNT" -- env NWPC_DATA_DIR="$DATA" "$PREFIX/venv/bin/python" -m nature_worker.platform_client init

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
ExecStart=$PREFIX/venv/bin/python -m nature_worker.platform_client serve
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
     "$PREFIX/venv/bin/python" -m nature_worker.platform_client preflight; then
  systemctl enable --now nature-worker-platform-client.service
  echo "Platform Client running: open http://127.0.0.1:$PORT/"
else
  echo "Preflight blocked: the service was NOT started. Resolve the findings above." >&2
  exit 1
fi
