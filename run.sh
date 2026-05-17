#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Install deps if needed
if [ ! -d ".venv" ]; then
  echo "Installing dependencies..."
  uv sync
fi

# Start proxy in background
echo "Starting proxy on http://127.0.0.1:8082 (DeepSeek)..."
uv run uvicorn server:app --host 127.0.0.1 --port 8082 --log-level warning &
PROXY_PID=$!

# Give it a moment to bind
sleep 1

# Verify it's up
if ! curl -sf http://127.0.0.1:8082/health > /dev/null; then
  echo "Proxy failed to start — check proxy.log"
  kill $PROXY_PID 2>/dev/null
  exit 1
fi

echo "Proxy running (pid $PROXY_PID)"
echo "Launching Claude Code → DeepSeek..."
echo ""

# Launch claude with proxy env vars — your real ~/.claude config is untouched
ANTHROPIC_BASE_URL=http://127.0.0.1:8082 \
ANTHROPIC_API_KEY=proxy \
claude "$@"

# Clean up proxy on exit
echo ""
echo "Shutting down proxy..."
kill $PROXY_PID 2>/dev/null
wait $PROXY_PID 2>/dev/null
echo "Done."
