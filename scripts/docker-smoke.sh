#!/usr/bin/env bash
# Docker verification for the documented Compose deployment (README, Option B).
#
# Builds the image and checks: /api/health and the container HEALTHCHECK, the
# mounted workspace, file ownership and permissions, and that the database,
# settings and stored key survive replacing the container.
#
# No model provider is contacted: the configured endpoint is unreachable and
# the key is a dummy. Runs in a temporary copy of the build files, so a local
# run never touches ./data or ./workspace. Usage: scripts/docker-smoke.sh
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
PORT=${SMOKE_PORT:-18080}
BASE="http://127.0.0.1:$PORT"
PROJECT=nature-worker-smoke
DUMMY_KEY="sk-docker-smoke-DUMMY-0000"
export NATURE_WORKER_PORT=$PORT NATURE_WORKER_UID=$(id -u) NATURE_WORKER_GID=$(id -g)

compose() { docker compose -p "$PROJECT" --project-directory "$WORK" -f "$WORK/compose.yaml" "$@"; }
fail() { echo "FAIL: $*" >&2; compose logs --no-color >&2 || true; exit 1; }
pass() { echo "PASS: $*"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
api() { curl -fsS "$@"; }

cleanup() { compose down --remove-orphans >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

wait_healthy() {
  for _ in $(seq 60); do
    curl -fsS "$BASE/api/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  fail "/api/health did not respond within 60 s"
}

cp -r "$ROOT"/{Dockerfile,compose.yaml,.dockerignore,requirements.lock,pyproject.toml,README.md,LICENSE,src} "$WORK"/
mkdir "$WORK/data" "$WORK/workspace"
echo "hello from the host" > "$WORK/workspace/hello.txt"
docker compose version

# -- build and start ---------------------------------------------------------------
compose build
compose up -d
wait_healthy
health=$(api "$BASE/api/health")
[ "$(echo "$health" | json 'd["ok"], d["version"]')" = "True 0.1.0" ] || fail "unexpected health response: $health"
pass "/api/health: $health"

# -- container user and permissions --------------------------------------------------
[ "$(compose exec -T worker id -u):$(compose exec -T worker id -g)" = "$NATURE_WORKER_UID:$NATURE_WORKER_GID" ] \
  || fail "container does not run as $NATURE_WORKER_UID:$NATURE_WORKER_GID"
[ "$(compose exec -T worker id -u)" != 0 ] || fail "container runs as root"
pass "container runs as non-root $NATURE_WORKER_UID:$NATURE_WORKER_GID"
if compose exec -T worker sh -c 'touch /app/probe' 2>/dev/null; then fail "application directory is writable"; fi
pass "application files are not writable by the container user"

# -- mounted workspace -----------------------------------------------------------------
files=$(api "$BASE/api/files")
[ "$(echo "$files" | json 'd["root"]')" = "/workspace" ] || fail "workspace root: $files"
echo "$files" | json '[f["path"] for f in d["files"]]' | grep -q "hello.txt" || fail "host file not visible: $files"
[ "$(api "$BASE/api/files/content?path=hello.txt" | json 'd["content"]')" = "hello from the host" ] || fail "host file content"
api -X PUT --data-binary "written by the container" "$BASE/api/files?path=from-container.txt" >/dev/null
[ "$(cat "$WORK/workspace/from-container.txt")" = "written by the container" ] || fail "container write not visible on host"
[ "$(stat -c %u "$WORK/workspace/from-container.txt")" = "$NATURE_WORKER_UID" ] || fail "written file not owned by host user"
pass "workspace mount: host file readable, container write lands on host owned by $NATURE_WORKER_UID"

# -- settings, stored key and a conversation ------------------------------------------
api -X PUT -H "content-type: application/json" "$BASE/api/settings" -d "{\"provider\": \"openai_compatible\",
  \"model\": \"smoke-model\", \"endpoint\": \"http://127.0.0.1:9/v1\", \"api_key\": \"$DUMMY_KEY\"}" >/dev/null
conv=$(api -X POST "$BASE/api/conversations" | json 'd["id"]')
status=$(api -X POST -H "content-type: application/json" "$BASE/api/conversations/$conv/messages" \
  -d '{"text": "Say hello."}' | json 'd["status"]')
[ "$status" = "error" ] || fail "expected a provider error for the unreachable endpoint, got $status"
pass "unreachable provider reported as an error notice (no model contacted)"

# -- data directory permissions -------------------------------------------------------------
for f in data:700 data/credentials.enc:600 data/master.key:600; do
  path=${f%%:*} mode=${f##*:}
  [ "$(stat -c %a "$WORK/$path")" = "$mode" ] || fail "$path has mode $(stat -c %a "$WORK/$path"), expected $mode"
done
[ "$(stat -c %u "$WORK/data/worker.db")" = "$NATURE_WORKER_UID" ] || fail "worker.db not owned by host user"
if grep -rqa "$DUMMY_KEY" "$WORK/data"; then fail "key stored in plain text"; fi
pass "data dir 700, credentials.enc and master.key 600, worker.db owned by host user, key encrypted at rest"

# -- HEALTHCHECK ----------------------------------------------------------------------------
cid=$(compose ps -q worker)
for _ in $(seq 90); do
  state=$(docker inspect --format '{{.State.Health.Status}}' "$cid")
  [ "$state" = healthy ] && break
  sleep 1
done
[ "$state" = healthy ] || fail "Docker HEALTHCHECK status is $state"
pass "Docker HEALTHCHECK: healthy"

# -- persistence across a new container ------------------------------------------------------
compose down
compose up -d
wait_healthy
[ "$(compose ps -q worker)" != "$cid" ] || fail "container was not replaced"
conv_after=$(api "$BASE/api/conversations/$conv")
[ "$(echo "$conv_after" | json 'len(d["messages"])')" -ge 2 ] || fail "conversation lost: $conv_after"
settings=$(api "$BASE/api/settings")
[ "$(echo "$settings" | json 'd["model"], d["api_key_source"]')" = "smoke-model stored" ] || fail "settings lost: $settings"
api "$BASE/api/files" | json '[f["path"] for f in d["files"]]' | grep -q "from-container.txt" || fail "workspace file lost"
pass "after replacing the container: conversation, settings, stored key and workspace files persist"

if compose logs --no-color | grep -q "$DUMMY_KEY"; then fail "key appeared in container logs"; fi
pass "key absent from container logs"
echo "Docker verification passed."
