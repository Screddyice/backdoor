#!/usr/bin/env bash
# build.sh — build the custom qwen3.5 tags WITH the shared system prompt baked in.
#
# Modelfiles have no include mechanism, so instead of duplicating the ~186KB
# prompt into every Modelfile, this script assembles each one at build time:
#   <variant>.Modelfile  +  SYSTEM """<prompts/claude-fable-5-system.md>"""
# and runs `ollama create` on the result.
#
# The SYSTEM directive is the model's DEFAULT system prompt: it applies to bare
# usage (`ollama run qwen3.5:4b-64k`, API calls with no system message). Any
# request that carries its own system message — i.e. every Claude Code session
# through the backdoor proxy — overrides it, so the harness is unaffected.
#
# NOT covered here (deliberately):
#   - qwen2.5-coder:32b / :14b — stock 32K-ctx tags; the ~46K-token prompt
#     doesn't fit their window.
#   - phi4 / gemma3:12b / llama3.1:8b — llm-jury council verifiers; a persona
#     prompt would interfere with the council's task prompts.
#
# Usage: ./build.sh [variant ...]     (default: all qwen3.5-*.Modelfile)
set -euo pipefail
cd "$(dirname "$0")"

PROMPT_FILE="../prompts/claude-fable-5-system.md"
[ -f "$PROMPT_FILE" ] || { echo "ERROR: $PROMPT_FILE not found" >&2; exit 1; }
if grep -q '"""' "$PROMPT_FILE"; then
  echo 'ERROR: prompt contains """ — would terminate the SYSTEM block early' >&2
  exit 1
fi

MODELFILES=("$@")
[ ${#MODELFILES[@]} -eq 0 ] && MODELFILES=(qwen3.5-*.Modelfile)

for mf in "${MODELFILES[@]}"; do
  [ -f "$mf" ] || { echo "ERROR: $mf not found" >&2; exit 1; }
  tag="${mf%.Modelfile}"
  tag="${tag/qwen3.5-/qwen3.5:}"
  tmp="$(mktemp)"
  {
    cat "$mf"
    printf '\nSYSTEM """\n'
    cat "$PROMPT_FILE"
    printf '"""\n'
  } > "$tmp"
  echo "▶ ollama create $tag  ($mf + system prompt)"
  ollama create "$tag" -f "$tmp"
  rm -f "$tmp"
done
echo "✓ done"
