#!/usr/bin/env bash
# Runs the three services + web gateway in one process group. If any process exits, everything stops
# (so a container platform restarts the whole app instead of leaving it half-alive).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${VENV:-$ROOT/.venv}/bin/python"
BRAIN_PORT="${BRAIN_PORT:-8001}"
INBOX_PORT="${INBOX_PORT:-8002}"
SCOPE_PORT="${SCOPE_PORT:-8003}"
export BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:$BRAIN_PORT}"
export INBOX_URL="${INBOX_URL:-http://127.0.0.1:$INBOX_PORT}"
export SCOPE_URL="${SCOPE_URL:-http://127.0.0.1:$SCOPE_PORT}"
export INBOX_PORT

prefix() { sed -u "s/^/[$1] /"; }
cleanup() { trap - INT TERM EXIT; kill 0 2>/dev/null; }
trap cleanup INT TERM EXIT

"$PY" -m uvicorn brain.api:create_app --factory --app-dir knowledge_brain --host 127.0.0.1 --port "$BRAIN_PORT" 2>&1 | prefix brain &
"$PY" -m uvicorn scope.api:create_app --factory --app-dir scope_builder --host 127.0.0.1 --port "$SCOPE_PORT" 2>&1 | prefix scope &
(cd inbox_agent && node --no-warnings=ExperimentalWarning dist/server.js) 2>&1 | prefix inbox &
node web/server.mjs 2>&1 | prefix web &

echo "AI Tech Consultant is starting: open http://localhost:${PORT:-8080}"
wait -n
echo "A service exited; stopping the rest." >&2
exit 1
