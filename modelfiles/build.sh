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
#   - Any tag whose name has no suffix after the colon (qwen3.5:9b, qwen3.5:4b).
#     Those collide with the upstream registry names and get SKIPPED — see the
#     guard in the loop. Both were being clobbered until 2026-08-16.
#   - Tags listed in BARE_TAGS, which build without the prompt.
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

# Tags that must NEVER carry the baked prompt, even though they live here.
#
# qwen3.5:9b-64k backs `/model qwen-9b` and the fusion-qwen subagent. Those
# always send their own system message, so the baked prompt bought them nothing
# and the only consumer it could have served — bare `ollama run` — was
# unusable on a 9B: 43,092 tokens of prefill, 8+ minutes, then a 500. Measured
# 2026-08-16, and 12 tokens / 2.4s once stripped. It kept the FROM-inheritance
# footgun with no upside, so this tag is built plain.
BARE_TAGS=("qwen3.5:9b-64k")

MODELFILES=("$@")
[ ${#MODELFILES[@]} -eq 0 ] && MODELFILES=(*.Modelfile)

for mf in "${MODELFILES[@]}"; do
  [ -f "$mf" ] || { echo "ERROR: $mf not found" >&2; exit 1; }
  tag="${mf%.Modelfile}"
  tag="${tag/-/:}"

  # NEVER CLOBBER AN UPSTREAM REGISTRY TAG.
  #
  # The tag is the filename with the first "-" turned into ":", so
  # qwen3.5-9b.Modelfile produced `qwen3.5:9b` — the exact name `ollama pull`
  # uses for the pristine base. Building it overwrote the registry pull with a
  # prompt-baked copy, and then every `FROM qwen3.5:9b` in this directory
  # inherited 43K tokens. That is the same inheritance bug the bare/ Modelfile
  # documents (the model answered from the baked prompt's tool vocabulary and
  # invented a `weather_fetch` tool), except the poisoned base made it invisible:
  # the Modelfile you read has no SYSTEM line anywhere in it.
  #
  # Found 2026-08-16 with `ollama show --system qwen3.5:9b` returning 186,647
  # bytes when it should return 0. Fix was `ollama pull qwen3.5:9b`, which took
  # 4 seconds because the model blob was already local and only the manifest
  # changed. A variant tag always has a suffix after the colon; a bare one does
  # not, and that is the whole test.
  if [[ "${tag#*:}" != *-* ]]; then
    echo "⚠ SKIP $mf → $tag — would overwrite the upstream registry tag." >&2
    echo "  Rename it to a variant (e.g. ${tag}-fable) if you want a persona build." >&2
    continue
  fi

  if [[ " ${BARE_TAGS[*]} " == *" $tag "* ]]; then
    echo "▶ ollama create $tag  ($mf, NO system prompt — see BARE_TAGS)"
    ollama create "$tag" -f "$mf"
    continue
  fi

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
