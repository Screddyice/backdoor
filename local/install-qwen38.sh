#!/bin/zsh
# install-qwen38.sh — wire the Qwen3.8-27B Action-Abliterated MLX tier into this
# host. Idempotent; safe to re-run after a re-vendor.
#
# Prerequisites, in order:
#   1. hf auth login                    (the HF repo is private)
#   2. HF_HUB_DISABLE_XET=1 hf download ajs-ai/Qwen3.8-27B-Action-Abliterated-MLX-4bit
#      The xet backend (hf-xet 1.6.0) HANGS at zero bytes on this host — it is
#      not a slow link, it is a stall. Disable it. Also unset HTTPS_PROXY: the
#      backdoor forward proxy on :8084 is in NO_PROXY for github.com but not for
#      huggingface.co, and a 16GB pull through mitmproxy crawls.
#   3. uv tool install mlx-vlm          (provides ~/.local/bin/mlx_vlm.server)
#   4. this script
#
# Then: `qwen38 start`, and `/model qwen38-action` in a routed session.
set -eu

readonly REPO_ID="ajs-ai/Qwen3.8-27B-Action-Abliterated-MLX-4bit"
readonly CACHE_REPO="${HOME}/.cache/huggingface/hub/models--ajs-ai--Qwen3.8-27B-Action-Abliterated-MLX-4bit"
readonly STABLE_LINK="${HOME}/Models/Qwen3.8-27B-Action-Abliterated-MLX-4bit-v1"
readonly HERE="${0:A:h}"

die() { print -u2 "$@"; exit 1; }

# 1. Locate the weights. Two shapes are supported, because `hf download` is not
#    reliable on a slow link: it aborts the whole job on one httpx.ReadTimeout
#    and deletes its own partials. The fallback is a per-file curl with -C -,
#    which lands a plain directory rather than a cache snapshot.
#      a) $STABLE_LINK is already a real directory holding the weights.
#      b) the HF cache holds a snapshot, and $STABLE_LINK becomes a symlink.
if [[ -d "$STABLE_LINK" && ! -L "$STABLE_LINK" && -e "$STABLE_LINK/config.json" ]]; then
  snapshot="$STABLE_LINK"
  print "Using weights already at $STABLE_LINK"
else
  [[ -d "$CACHE_REPO/snapshots" ]] || die "Model not downloaded. See step 2 in the header."
  snapshot="$(find "$CACHE_REPO/snapshots" -maxdepth 1 -mindepth 1 -type d | head -1)"
  [[ -n "$snapshot" ]] || die "No snapshot under $CACHE_REPO/snapshots"
  mkdir -p "${HOME}/Models"
  ln -sfn "$snapshot" "$STABLE_LINK"
  print "Model pinned: $STABLE_LINK -> $snapshot"
fi

for required in config.json model.safetensors.index.json tokenizer.json; do
  [[ -e "$snapshot/$required" ]] || die "Weights are missing $required — download incomplete."
done
shards="$(find "$snapshot" -name 'model-*-of-*.safetensors' | wc -l | tr -d ' ')"
[[ "$shards" == "3" ]] || die "Expected 3 safetensors shards, found $shards — download incomplete."

# 2. Verify against the manifest the artifact ships with, when present. The
#    upstream README verified this 15-file artifact byte-for-byte against its
#    cloud SHA256SUMS; do the same here rather than trusting the transfer.
if [[ -f "$snapshot/SOURCE_SHA256SUMS" ]]; then
  print "Verifying SOURCE_SHA256SUMS (this reads ~15GB, give it a minute)..."
  ( cd "$snapshot" && shasum -a 256 -c SOURCE_SHA256SUMS --quiet ) \
    && print "Checksums OK." \
    || die "CHECKSUM MISMATCH. Do not serve this artifact; re-download it."
else
  print -u2 "WARNING: no SOURCE_SHA256SUMS in the snapshot; skipping verification."
fi

# 3. mlx_vlm.server must exist at the path baked into the plists.
[[ -x "${HOME}/.local/bin/mlx_vlm.server" ]] \
  || die "Missing ${HOME}/.local/bin/mlx_vlm.server — run: uv tool install mlx-vlm"

# 4. LaunchAgents + helper on PATH.
mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/Library/Logs" "${HOME}/.local/bin"
for plist in com.aicollective.qwen38-mlx com.aicollective.qwen38-mlx-long; do
  cp "$HERE/$plist.plist" "${HOME}/Library/LaunchAgents/$plist.plist"
  # Re-bootstrap so an edited plist is actually picked up; launchd caches.
  launchctl bootout "gui/$(id -u)/$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "${HOME}/Library/LaunchAgents/$plist.plist" 2>/dev/null || true
  print "Installed $plist"
done
ln -sfn "$HERE/qwen38" "${HOME}/.local/bin/qwen38"
print "Helper: ${HOME}/.local/bin/qwen38"

print ""
print "Done. Next: qwen38 start && qwen38 status"
print "Remember: qwen38 stop before any llm-jury council run — this server holds"
print "~19GB that Ollama cannot evict."
