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
# THE OPPOSITE SHAPE ALSO SHOWS UP, and it broke this script once. qwen3.8:27b
# is packaged as FOUR blobs, one of them a single 16.81GB model layer. There the
# concurrency does nothing (one blob = one worker) and the thing that matters is
# resume: `ollama pull` stalled it at 0.93GB on 2026-08-16 and never recovered.
# So this script now resumes with `curl -C -` and detects a dead transfer by
# throughput rather than by a wall-clock cap — see fetch_one.
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

# A single huge blob gets exactly ONE worker from the xargs pool above, so the
# whole model moves at one connection's speed. The registry throttles per
# connection: measured 2026-08-16 on the qwen3.8:27b model layer, one stream ran
# 435KB/s while four ranged streams aggregated ~1.0MB/s. Roughly 2x, not 4x —
# there is a ceiling above the per-connection limit — but 2x on a 16.81GB blob
# is hours.
#
# So blobs past the threshold are fetched as parallel byte ranges and
# concatenated. Each chunk is its own resumable file, so an interrupted run
# restarts only the chunks that were mid-flight. The SHA-256 check on the
# reassembled blob is what makes this safe: a misordered or short concat cannot
# pass it, and a failed check throws the whole blob away rather than installing
# it.
BIG_BLOB_BYTES=${BIG_BLOB_BYTES:-1073741824}   # 1GB
CHUNK_BYTES=${CHUNK_BYTES:-67108864}           # 64MB
CHUNK_JOBS=${CHUNK_JOBS:-6}

fetch_chunk() {
    url="$1"; start="$2"; end="$3"; out="$4"; want=$(( end - start + 1 ))
    if [ -f "$out" ] && [ "$(stat -f%z "$out" 2>/dev/null)" = "$want" ]; then return 0; fi
    for a in 1 2 3 4 5 6; do
        rm -f "$out"
        if curl -sfL -r "${start}-${end}" --connect-timeout 15 \
                --speed-limit 20480 --speed-time 60 -o "$out" "$url" 2>/dev/null &&
           [ "$(stat -f%z "$out" 2>/dev/null)" = "$want" ]; then
            return 0
        fi
        sleep $a
    done
    return 1
}
export -f fetch_chunk

fetch_big() {
    dig="$1"; size="$2"; hex="${dig#sha256:}"
    dest="$HOME/.ollama/models/blobs/sha256-$hex"
    cdir="$dest.chunks"; mkdir -p "$cdir"
    url="$REG_URL/blobs/$dig"
    n=$(( (size + CHUNK_BYTES - 1) / CHUNK_BYTES ))
    echo "  big blob ${hex:0:12} — $((size/1000000))MB in $n chunks, $CHUNK_JOBS at a time" >&2
    : > /tmp/.ollama_chunks.$$
    i=0
    while [ "$i" -lt "$n" ]; do
        s=$(( i * CHUNK_BYTES )); e=$(( s + CHUNK_BYTES - 1 ))
        [ "$e" -ge "$size" ] && e=$(( size - 1 ))
        printf '%s %s %s %s\n' "$url" "$s" "$e" "$cdir/$(printf '%06d' $i)" >> /tmp/.ollama_chunks.$$
        i=$(( i + 1 ))
    done
    xargs -P "$CHUNK_JOBS" -n 4 bash -c 'fetch_chunk "$0" "$1" "$2" "$3"' < /tmp/.ollama_chunks.$$
    rm -f /tmp/.ollama_chunks.$$

    # Pre-check the total before touching anything. A plain `cat` of short or
    # missing chunks produces a wrong blob that only the SHA catches, and by
    # then the disk cost has already been paid. Cheap arithmetic first.
    have=0
    for f in "$cdir"/[0-9]*; do
        [ -f "$f" ] && have=$(( have + $(stat -f%z "$f") ))
    done
    if [ "$have" != "$size" ]; then
        echo "  big blob ${hex:0:12}: have $((have/1000000))MB of $((size/1000000))MB — rerun to resume" >&2
        return 1
    fi

    # Concatenate chunk by chunk and DELETE EACH CHUNK AS IT LANDS.
    #
    # The obvious `cat "$cdir"/* > "$dest.part"` needs the blob twice on disk at
    # once: 16.81GB of chunks plus a 16.81GB reassembly. This host was at 14GB
    # free when it got here, so that plain cat cannot run. Appending and
    # unlinking keeps the peak at one chunk (64MB) above steady state.
    #
    # Safe because the size pre-check above already ran: the only thing the SHA
    # can still catch is bad bytes, not missing ones, and the chunks are
    # reproducible from the registry either way.
    : > "$dest.part"
    for f in "$cdir"/[0-9]*; do
        cat "$f" >> "$dest.part" || { echo "  concat failed at $f" >&2; return 1; }
        rm -f "$f"
    done
    if [ "$(stat -f%z "$dest.part" 2>/dev/null)" = "$size" ] &&
       [ "$(shasum -a 256 "$dest.part" | cut -d' ' -f1)" = "$hex" ]; then
        mv "$dest.part" "$dest"; rm -rf "$cdir"; return 0
    fi
    rm -f "$dest.part"
    echo "  big blob ${hex:0:12} failed verification — rerun to re-fetch" >&2
    return 1
}

fetch_one() {
    dig="$1"; size="$2"
    hex="${dig#sha256:}"
    dest="$HOME/.ollama/models/blobs/sha256-$hex"
    # Stable .part name (no $$): a partial must survive the script exiting so a
    # rerun resumes instead of restarting. Workers never share a digest, so
    # concurrent runs cannot collide on one .part.
    part="$dest.part"
    if [ -f "$dest" ] && [ "$(stat -f%z "$dest")" = "$size" ]; then echo "have"; return 0; fi
    if [ "$size" -ge "$BIG_BLOB_BYTES" ]; then fetch_big "$dig" "$size"; return $?; fi
    # A .part longer than the blob is corrupt, and -C - would happily append to
    # it forever. Truncating here is what keeps a bad resume from looping.
    if [ -f "$part" ] && [ "$(stat -f%z "$part" 2>/dev/null)" -gt "$size" ]; then
        rm -f "$part"
    fi
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        # -C - resumes from whatever is already in .part.
        #
        # Stall detection replaces a fixed --max-time. --max-time 240 was right
        # when every blob was a few MB (qwen3.5:27b-int4's 1,184 tensor layers)
        # and wrong the moment a model shipped as ONE big blob: qwen3.8:27b is a
        # single 16.81GB layer, which cannot transfer inside 240s at any speed
        # this network reaches, so every attempt timed out mid-download and the
        # old `rm -f "$part"` threw the bytes away before retrying. That is an
        # infinite loop that looks like a slow network.
        #
        # --speed-limit/--speed-time abort only when throughput actually dies
        # (<50KB/s sustained for 60s), which is the real failure being guarded
        # against, and is independent of blob size.
        if curl -sfL -C - --connect-timeout 15 \
                --speed-limit 51200 --speed-time 60 \
                -o "$part" "$REG_URL/blobs/$dig" 2>/dev/null; then
            if [ "$(stat -f%z "$part" 2>/dev/null)" = "$size" ]; then
                actual=$(shasum -a 256 "$part" | cut -d' ' -f1)
                if [ "$actual" = "$hex" ]; then mv "$part" "$dest"; echo "got"; return 0; fi
                # Right length, wrong hash: the resume glued together garbage.
                # Start this blob over rather than resume onto a bad prefix.
                rm -f "$part"
            fi
        fi
        # OVERSIZE MUST BE CHECKED EVERY ATTEMPT, not just before the loop.
        # If the server ignores the Range header and replays the whole body,
        # `-C -` appends it to what is already there and the file grows past
        # $size. The equality test above then never matches, so the next attempt
        # appends again. Measured on the qwen3.8 projector blob: 931,146,016
        # bytes expected, 1.74GB on disk after the retries, and the loop was
        # still going. Truncating here is what stops a resume from running away.
        if [ -f "$part" ] && [ "$(stat -f%z "$part" 2>/dev/null)" -gt "$size" ]; then
            rm -f "$part"
        fi
        sleep $((attempt))
    done
    echo "FAIL $hex" >&2
    return 1
}
export -f fetch_one
# fetch_one runs in an xargs `bash -c` subshell, so everything it reaches for
# must cross that boundary explicitly — including fetch_big, which it calls, and
# fetch_big's own tuning vars. Exporting fetch_chunk alone is not enough.
export -f fetch_big
export BIG_BLOB_BYTES CHUNK_BYTES CHUNK_JOBS
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
