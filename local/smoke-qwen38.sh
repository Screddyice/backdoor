#!/bin/zsh
# smoke-qwen38.sh — prove the MLX tier actually serves before trusting it.
#
# Run after local/install-qwen38.sh. Checks four things, in the order that
# fails cheapest first:
#   1. the server answers /health
#   2. it generates text at all
#   3. it CALLS TOOLS. This is the one that matters and the one that fails
#      quietly: a tier that stops calling tools looks healthy right up until
#      failover hands it real work. Every other profile in this repo documents
#      the same check for the same reason.
#   4. the router reaches it through the qwen route
set -uo pipefail

readonly BASE="http://127.0.0.1:8080/v1"
# Must match what the server loaded; it resolves this as a repo_id.
readonly MODEL="/Users/screddy/Models/Qwen3.8-27B-Action-Abliterated-MLX-4bit-v1"
fails=0

step() { print "\n=== $1 ==="; }
ok()   { print "PASS: $1"; }
bad()  { print -u2 "FAIL: $1"; fails=$((fails + 1)); }

step "1. health"
if curl -fsS --max-time 5 http://127.0.0.1:8080/health >/dev/null 2>&1; then
  ok "server is up"
else
  bad "no server on 8080. Run: qwen38 start"
  print -u2 "Stopping here; the rest of the checks need a live server."
  exit 1
fi

step "2. generation"
gen="$(curl -sS --max-time 300 "$BASE/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: OK\"}]}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"][:200])' 2>/dev/null)"
if [[ -n "$gen" ]]; then ok "generated: ${gen//$'\n'/ }"; else bad "no completion came back"; fi

step "3. tool calling"
tools='[{"type":"function","function":{"name":"get_weather","description":"Get weather for a city","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]'
calls="$(curl -sS --max-time 300 "$BASE/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":256,\"messages\":[{\"role\":\"user\",\"content\":\"Weather in Paris? Use the tool.\"}],\"tools\":$tools}" \
  | python3 -c 'import json,sys; m=json.load(sys.stdin)["choices"][0]["message"]; print(json.dumps(m.get("tool_calls")))' 2>/dev/null)"
if [[ -n "$calls" && "$calls" != "null" ]]; then
  ok "tool_calls: $calls"
else
  bad "no tool_calls. failover_keep_tools passes tool definitions to this tier;"
  print -u2 "      a tier that cannot call them cannot do offline work."
fi

step "4. router path"
if curl -fsS --max-time 5 http://127.0.0.1:8083/health >/dev/null 2>&1; then
  ok "router is up on 8083 (check proxy.log for '→ local [qwen → local-qwen38-action]')"
else
  print "SKIP: router not running on 8083"
fi

print ""
if (( fails == 0 )); then
  print "All checks passed. The tier is serving."
else
  print -u2 "$fails check(s) failed. Do not mark the tier verified."
  exit 1
fi
