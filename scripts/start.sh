#!/usr/bin/env bash
# One-command local start: installs what is missing, builds, and runs everything.
#   ./scripts/start.sh            then open http://localhost:8080
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Add GEMINI_API_KEY to use the AI models (the app runs in labelled offline mode without it)."
fi

PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
node -e 'const [a,b]=process.versions.node.split(".").map(Number); if (a<22||(a===22&&b<13)) { console.error("Node 22.13+ required"); process.exit(1) }'

stamp() { cat "$@" | sha256sum | cut -c1-16; }
if [ ! -x .venv/bin/python ]; then "$PYTHON" -m venv .venv; fi
want="$(stamp knowledge_brain/requirements.txt scope_builder/requirements.txt)"
if [ "$(cat .venv/.stamp 2>/dev/null)" != "$want" ]; then
  echo "Installing Python dependencies…"
  .venv/bin/pip install -q -r knowledge_brain/requirements.txt -r scope_builder/requirements.txt
  echo "$want" > .venv/.stamp
fi
for dir in inbox_agent web; do
  want="$(stamp "$dir/package-lock.json")"
  if [ "$(cat "$dir/node_modules/.stamp" 2>/dev/null)" != "$want" ]; then
    echo "Installing $dir dependencies…"
    (cd "$dir" && npm ci --silent)
    echo "$want" > "$dir/node_modules/.stamp"
  fi
done
echo "Building…"
(cd inbox_agent && npm run --silent build)
(cd web && npm run --silent build >/dev/null)

exec bash scripts/run-services.sh
