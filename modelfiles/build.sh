#!/usr/bin/env bash
# build.sh — build the local model tags WITH the shared system prompt baked in.
#
# Modelfiles have no include mechanism, so instead of duplicating the ~180KB
# prompt into every Modelfile, this script assembles each one at build time:
#   <variant>.Modelfile  +  SYSTEM """<prompts/claude-fable-5-system.md>"""
# and runs `ollama create`. Tag = filename minus .Modelfile, first "-" -> ":"
# (qwen3.5-4b-64k.Modelfile -> qwen3.5:4b-64k, llama3.1-8b-fable.Modelfile ->
# llama3.1:8b-fable).
#
# The SYSTEM directive is the model's DEFAULT system prompt (~43K tokens
# measured): it applies to bare usage (`ollama run <tag>`, API calls with no
# system message). Any request that carries its own system message — i.e.
# every Claude Code session through the backdoor proxy — overrides it, so the
# harness is unaffected.
#
# NOT covered here (deliberately):
#   - phi4 (16K native) and qwen3:8b (41K max) — the 43K-token prompt does not
#     fit their context windows at all.
#   - The CANONICAL llm-jury council tags (phi4, gemma3:12b, llama3.1:8b) stay
#     pristine: the jury sends bare user messages (no system role) at
#     num_ctx 8192, so a baked system prompt would overflow every council call
#     and break fusion. Persona use gets the *-fable variants instead
#     (llama3.1:8b-fable, gemma3:12b-fable).
#
# Loading note: tags here bake num_ctx 65536, and with OLLAMA_NUM_PARALLEL=4
# the server allocates KV for 4 x num_ctx on load — fine for the 4B, heavy
# (~16GB KV) for 9-12B models. The 8-12B *-fable variants are for deliberate
# persona sessions, not background use.
#
# Usage: ./build.sh [variant.Modelfile ...]     (default: all *.Modelfile)
set -euo pipefail
cd "$(dirname "$0")"

PROMPT_FILE="../prompts/claude-fable-5-system.md"
[ -f "$PROMPT_FILE" ] || { echo "ERROR: $PROMPT_FILE not found" >&2; exit 1; }
if grep -q '"""' "$PROMPT_FILE"; then
  echo 'ERROR: prompt contains """ — would terminate the SYSTEM block early' >&2
  exit 1
fi

MODELFILES=("$@")
[ ${#MODELFILES[@]} -eq 0 ] && MODELFILES=(*.Modelfile)

for mf in "${MODELFILES[@]}"; do
  [ -f "$mf" ] || { echo "ERROR: $mf not found" >&2; exit 1; }
  tag="${mf%.Modelfile}"
  tag="${tag/-/:}"
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
