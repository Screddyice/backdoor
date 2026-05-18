#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Validation ────────────────────────────────────────────────────────────────

if [ ! -f ".env" ]; then
  echo "ERROR: No .env file found."
  echo "Run: cp .env.example .env  — then fill in your provider details."
  exit 1
fi

# Load .env
set -a; source .env; set +a

if [ -z "$PROVIDER_API_KEY" ] || [ "$PROVIDER_API_KEY" = "your-api-key-here" ]; then
  echo "ERROR: PROVIDER_API_KEY is not set in .env."
  echo "Add your API key and try again."
  exit 1
fi

if [ -z "$PROVIDER_BASE_URL" ]; then
  echo "ERROR: PROVIDER_BASE_URL is not set in .env."
  exit 1
fi

if ! command -v claude &> /dev/null; then
  echo "ERROR: Claude Code is not installed."
  echo "Install it from: https://docs.anthropic.com/en/docs/claude-code"
  exit 1
fi

if ! command -v uv &> /dev/null; then
  echo "ERROR: uv is not installed."
  echo "Install it from: https://docs.astral.sh/uv/"
  exit 1
fi

# ── Setup ─────────────────────────────────────────────────────────────────────

if [ ! -d ".venv" ]; then
  echo "Installing dependencies (first run only)..."
  uv sync
fi

PORT="${PORT:-8082}"
PROVIDER_LABEL="${PROVIDER_MODEL:-unknown}"

# ── Start proxy ───────────────────────────────────────────────────────────────

echo "Starting Backdoor → $PROVIDER_LABEL"
uv run python -m uvicorn server:app --host 127.0.0.1 --port "$PORT" --log-level warning &
PROXY_PID=$!

# Wait for proxy to be ready
for i in {1..10}; do
  if curl -sf "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! curl -sf "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
  echo "ERROR: Proxy failed to start. Check proxy.log for details."
  kill $PROXY_PID 2>/dev/null
  exit 1
fi

echo "Proxy running on port $PORT. Launching Claude Code..."
echo ""

# ── Launch Claude Code ────────────────────────────────────────────────────────

ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT" \
ANTHROPIC_API_KEY=proxy \
claude "$@"

# ── Cleanup ───────────────────────────────────────────────────────────────────

echo ""
echo "Shutting down Backdoor..."
kill $PROXY_PID 2>/dev/null
wait $PROXY_PID 2>/dev/null
echo "Done."
