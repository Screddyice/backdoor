#!/usr/bin/env bash
# Fetch an ollama library model with parallel per-blob downloads.
#
# qwen3.5:27b-int4 is packaged as 1,193 layers (1,184 of them per-tensor), not
# as one 16GB blob. That is what defeats `ollama pull` here: 1,193 sequential
# transfers over a connection that drops, with OLLAMA_MAX_TRANSFER_STREAMS=1
# forbidding any overlap, so a single stall halts everything.
#
# Many small files means the cost is per-request latency, not bandwidth, so the
# fix is concurrency rather than bigger reads. Each blob is small enough that one
# request completes well inside the window that stays alive on this network.
#
# Safety: every blob is written to .part, SHA-256 verified, and only then moved
# into the content-addressed store. The manifest is written last, so `ollama
# list` cannot show a model whose tensors are missing.
set -uo pipefail

MODEL="${1:?usage: fetch_parallel.sh <model> <tag>}"
TAG="${2:?usage: fetch_parallel.sh <model> <tag>}"
JOBS="${3:-10}"
REG="https://registry.ollama.ai/v2/library/$MODEL"
STORE="$HOME/.ollama/models"
BLOBS="$STORE/blobs"
MANDIR="$STORE/manifests/registry.ollama.ai/library/$MODEL"
mkdir -p "$BLOBS" "$MANDIR"

man=$(curl -sfL -m 60 "$REG/manifests/$TAG")
[ -n "$man" ] || { echo "ERROR: empty manifest" >&2; exit 1; }
printf '%s' "$man" > /tmp/.ollama_fetch_manifest.json

printf '%s' "$man" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for l in [d['config']] + d['layers']:
    print(l['digest'], l['size'])
" > /tmp/.ollama_fetch_list

total=$(wc -l < /tmp/.ollama_fetch_list | tr -d ' ')
echo "manifest has $total blobs; fetching with $JOBS workers"

fetch_one() {
    dig="$1"; size="$2"
    hex="${dig#sha256:}"
    dest="$HOME/.ollama/models/blobs/sha256-$hex"
    part="$dest.part.$$"
    if [ -f "$dest" ] && [ "$(stat -f%z "$dest")" = "$size" ]; then echo "have"; return 0; fi
    for attempt in 1 2 3 4 5 6; do
        rm -f "$part"
        if curl -sfL --connect-timeout 15 --max-time 240 \
                -o "$part" "$REG_URL/blobs/$dig" 2>/dev/null; then
            if [ "$(stat -f%z "$part" 2>/dev/null)" = "$size" ]; then
                actual=$(shasum -a 256 "$part" | cut -d' ' -f1)
                if [ "$actual" = "$hex" ]; then mv "$part" "$dest"; echo "got"; return 0; fi
            fi
        fi
        sleep $((attempt))
    done
    rm -f "$part"
    echo "FAIL $hex" >&2
    return 1
}
export -f fetch_one
export REG_URL="$REG"

# xargs runs the workers; the counter below just reports progress cheaply.
tr ' ' '\n' < /dev/null  # no-op, keeps shellcheck quiet
xargs -P "$JOBS" -n 2 bash -c 'fetch_one "$0" "$1" >/dev/null' < /tmp/.ollama_fetch_list

missing=0
while read -r dig size; do
    hex="${dig#sha256:}"
    dest="$BLOBS/sha256-$hex"
    if [ ! -f "$dest" ] || [ "$(stat -f%z "$dest")" != "$size" ]; then
        missing=$((missing + 1))
    fi
done < /tmp/.ollama_fetch_list

if [ "$missing" -gt 0 ]; then
    echo "INCOMPLETE: $missing/$total blobs missing — rerun to resume" >&2
    exit 1
fi

printf '%s' "$man" > "$MANDIR/$TAG"
echo "INSTALLED $MODEL:$TAG ($total blobs)"
