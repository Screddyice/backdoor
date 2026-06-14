# Local / offline LLM setup (Mac)

Run Claude Code — with your full claude-harness — against a **local, open-source
model**, completely offline. No internet needed once the models are downloaded.

```
Claude Code  ->  backdoor proxy (:8082)  ->  Ollama (:11434)  ->  local model
   (UI +              (translates              (serves the         (Qwen3.5 4B,
    harness)           Anthropic<->OpenAI)      open weights)        on-device)
```

Everything runs on this Mac. The harness loads normally because it's a global
Claude Code plugin — the only thing that changes is the model underneath.

## One command

```bash
qwen            # FULL harness (default). Qwen3.5 4B @ 256K ctx, with
                # claude-harness + claude-mem + ralph-loop hooks ACTIVE.
                # ~50K-tok prompt. (Swapped down from 9B 2026-06-14 — the 9B
                # left no headroom for the full harness on the 36GB M5 Max.)
qwen lean       # Minimal (~945-tok) prompt on the 4B. Fastest; harness
                # slash-commands on demand, no auto hooks.
qwen coder      # Qwen2.5-Coder 32B (best raw code quality; 32K ctx; lean).
qwen fast       # Lean on the 14B coder. OPTIONAL: needs `ollama pull qwen2.5-coder:14b`.
```

(`claude-local` is a symlink alias for `qwen` if you prefer the longer name.)

**Why a small Qwen3.5 beats the 32B as the default:** Qwen3.5 is 256K-native
(no rope penalty) and has native tool-calling (no content-JSON fallback needed)
— which is what makes the FULL harness viable. The full-harness prompt is ~50K
tokens, which doesn't even fit the 32B's 32K window. The 4B (default since
2026-06-14) adds the decisive win: ~2.5GB weights leave plenty of headroom to
run the full harness + MCP alongside it, where the 9B at 256K loaded ~16GB and
left almost none on the 36GB M5 Max. Tradeoff: a 4B is weaker than the 9B —
scope agentic tasks tighter (see Reliability notes).

`qwen` makes sure the Ollama daemon is up, confirms the model is pulled,
points backdoor at the offline profile, and launches Claude Code. Anything after
the first word is passed straight to `claude` (e.g. `qwen fast --resume`).

## `/model qwen` in any terminal Claude Code session (hybrid router)

A second proxy instance runs permanently on **:8083** in `ROUTER_MODE=hybrid`
(LaunchAgent `com.screddy.backdoor-router`, KeepAlive). It routes by requested
model name: `qwen` / `qwen-coder` / `qwen-fast` → the matching local profile;
**every other model and endpoint passes through byte-faithfully to
api.anthropic.com** (auth headers, SSE, compression untouched).

`~/.zshrc` exports `ANTHROPIC_BASE_URL=http://127.0.0.1:8083` for terminal
shells (health-guarded: if the router is down, new shells fall back to direct
Anthropic). So in any normal terminal Claude Code session:

    /model qwen        # switch this session to the local Qwen3.5 4B
    /model qwen-coder  # the 32B coder
    claude --model qwen -p "..."   # one-shot

Notes:
- The offline `qwen` wrapper still pins :8082 explicitly — unaffected.
- GUI/IDE/cron Claude Code sessions don't read .zshrc → they stay direct cloud
  (no /model qwen there; launch from a terminal if you want it).
- Do NOT put ANTHROPIC_BASE_URL in ~/.claude/settings.json env — settings env
  OVERRIDES process env (verified), which would hijack the :8082 wrapper.
- Restart: `launchctl kickstart -k gui/$(id -u)/com.screddy.backdoor-router`;
  log: `router.log` in the repo.

## Manual control (the underlying `bd` CLI)

```bash
bd list                 # show backends (* = active)
bd switch local-qwen35  # Qwen3.5 4B @ 256K (default)
bd switch local-coder   # 32B coder offline
bd switch local-fast    # 14B coder offline
bd switch modal-qwen    # back to the Modal cloud backend (needs internet)
bd claude               # launch Claude Code through the active backend
bd status               # what's active + proxy health
bd stop                 # stop the proxy
```

## Profiles

| Profile         | Model               | Notes                                    |
|-----------------|---------------------|------------------------------------------|
| `local-qwen35`  | qwen3.5:4b-256k     | ~2.5GB weights. Tools+thinking. Light enough to run alongside the full harness. **Default.** |
| `local-coder`   | qwen2.5-coder:32b   | ~20GB. Best raw code quality. 32K ctx.   |
| `local-fast`    | qwen2.5-coder:14b   | ~9GB. Faster, lighter — good on battery. |
| `modal-qwen`    | (Modal cloud)       | Pre-existing cloud backend. Online only. |

`qwen3.5:4b-256k` is a local tag built from `modelfiles/qwen3.5-4b-256k.Modelfile`
(`FROM qwen3.5:4b` + `PARAMETER num_ctx 262144` — the full native context, kept
for full-MCP sessions: ~50K harness + curated MCP schemas + headroom; cheap on
the 4B). A 128K variant (`qwen3.5:4b-128k`) also exists for MCP-off use. Rebuild
after re-pulling:

```bash
ollama pull qwen3.5:4b
ollama create qwen3.5:4b-256k -f ~/backdoor/modelfiles/qwen3.5-4b-256k.Modelfile
ollama create qwen3.5:4b-128k -f ~/backdoor/modelfiles/qwen3.5-4b-128k.Modelfile
```

The 9B variants (`qwen3.5:9b-256k` / `-128k`, built from the matching 9B
Modelfiles) are still available if you want to switch back: re-pull `qwen3.5:9b`,
rebuild those tags, and set `PROVIDER_MODEL=qwen3.5:9b-256k` in the profile.

The num_ctx is baked into the model (not `OLLAMA_CONTEXT_LENGTH`) so the global
32768 default stays right for the 32B, whose KV cache would blow 36GB at 128K.
Thinking is disabled in the profile (`PROVIDER_REASONING_EFFORT=none`): with it
on, qwen3.5 intermittently ends a turn with reasoning ONLY (empty visible
message) and every turn pays reasoning latency. Blank the var to re-enable —
the proxy will surface reasoning as Anthropic thinking blocks, and promotes a
reasoning-only finish to text so turns are never empty.

## The pieces (so future-you can fix it)

- **Ollama daemon** runs via LaunchAgent `~/Library/LaunchAgents/com.screddy.ollama.plist`
  (RunAtLoad + KeepAlive). It sets the settings that make Claude Code actually work:
  - `OLLAMA_CONTEXT_LENGTH=32768` — Claude Code's system prompt + tool defs are big;
    the default ~4k context silently truncates them and breaks tool-calling. **This is
    the #1 gotcha** — don't lower it.
  - `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0` — halve KV-cache memory so
    32k context on the 32B model stays comfortable on 36GB unified memory.
  - `OLLAMA_KEEP_ALIVE=5m` — unload the model 5 min after last use to free RAM/battery.
  - Restart it after edits: `launchctl kickstart -k gui/$(id -u)/com.screddy.ollama`
- **backdoor proxy** lives at `~/backdoor` (symlink to
  `~/projects/Screddyice/backdoor`). The `bd` CLI and `claude-local` wrapper
  are in `~/.local/bin`.
- **Profiles** are `profiles/local-qwen35.env`, `local-coder.env`, `local-fast.env`.
  `PROVIDER_API_KEY=ollama` is a dummy (Ollama ignores it; bd just refuses an empty key).

## Updating / adding models (needs internet)

```bash
ollama pull qwen3.5:4b             # re-pull / update the default (then re-create the -256k/-128k tags)
ollama pull qwen2.5-coder:32b      # re-pull the coder
ollama pull <some-other-model>     # then add a profile pointing PROVIDER_MODEL at it
ollama list                        # see what's available offline
```

## Proxy patches that make local models drive Claude Code

All in `src/proxy/`, committed to the Screddyice/backdoor repo:

1. **`translate.py` — content-embedded tool-call fallback** (qwen2.5-coder).
   Detects bare-JSON tool calls in message content (validated against the
   request's tool names) and converts them to real `tool_use` blocks, in both
   non-streaming and streaming paths. Qwen3.5 doesn't need this (native
   tool_calls), but the 32B coder profile still does.
2. **`models.py` — accept non-user/assistant roles.** `Message.role` is `str`,
   not `Literal["user","assistant"]` — Claude Code sends other roles.
3. **`translate.py` — billing-header strip (THE cache fix).** Claude Code
   prepends `x-anthropic-billing-header: ... cch=<hash>;` to the system prompt
   with a hash that CHANGES EVERY REQUEST. One changing token at position ~30
   invalidates Ollama's entire KV prefix cache → full ~50K re-prefill (~2 min)
   on EVERY turn. The proxy strips that line; with it gone, turn 2+ of a
   session reuses the prefix and takes ~8s.
4. **`translate.py` — reasoning → thinking blocks.** Ollama's `reasoning`
   field (qwen3.x thinking) is converted to Anthropic thinking blocks instead
   of dropped, and a reasoning-only finish is promoted to text so the
   assistant message is never empty. Thinking blocks are stripped from history
   on the way back to OpenAI format.
5. **`routes.py` — eager message_start + 15s heartbeat pings.** During a long
   prefill Ollama emits zero bytes; without the heartbeat the byte-silent
   stream gets killed and retried, doubling every cold prefill.
6. **`client.py` — upstream read timeout 120s → 600s.** 120s killed any
   prefill over 2 minutes (this was the actual source of the kill/retry loop).

## MCP defaults: ON in full mode, OFF in lean/coder/fast

Full mode (`qwen`) now keeps this machine's global MCP servers — **~420 tools /
~148K tokens** of schemas — enabled by default. This is why the default profile
runs the 256K-ctx model tag; expect a long first-turn prefill when the schemas
aren't in Ollama's KV cache yet. MCP servers need internet, so full mode is no
longer true-offline; `QWEN_MCP=0 qwen` restores the offline behavior
(`--strict-mcp-config --mcp-config ~/backdoor/empty-mcp.json`).

Lean/coder/fast modes stay MCP-OFF (minimal prompt is the point, and the
qwen2.5 coders only have 32K ctx — 4.5× too small for the schemas). Override
with `QWEN_MCP=1`.

## Full harness is the default (lean is the speed escape hatch)

On the 32B, prompt size made full mode unusable (minutes/turn), so lean was the
default. With the billing-header cache fix, full mode is fast enough on the
small Qwen3.5 models (the 9B measured ~40s first turn / ~8s warm turns; the 4B
default is lighter and at least as fast) — so the full harness (hooks,
auto-memory, briefing, loop) is the default.

`qwen lean` still launches with:

    claude --bare --plugin-dir <claude-harness>  --strict-mcp-config ...

- `--bare` skips the heavy plugin/hook auto-injection → prompt drops to ~945 tokens.
- `--plugin-dir <harness>` re-adds the claude-harness plugin so its slash-commands
  (`/claude-harness:flow`, …) are available on demand — you just don't get the
  automatic hook behaviours (auto-memory, briefing injection).

Measured: **lean ≈ 945 tokens / ~6s per turn**; full ≈ 50K tokens, ~40s first
turn / ~8s warm turns (KV prefix cache).

`qwen` execs `claude` directly (not `bd claude`) so it runs in **your current
directory** — `bd claude` would `cd` into the backdoor repo and break file paths.

### claude-mem (memory) — verified working in `qwen full`
- **Capture:** VERIFIED — a full-mode session's prompt landed in `~/.claude-mem/claude-mem.db`
  (`observations` + `user_prompts` grew). claude-mem's hooks fire normally in full mode.
- **Recall:** memory auto-injects at session start (project-scoped; a brand-new dir
  injects little, a dir with history injects its observations).
- **Footprint:** claude-mem itself is light (~223 tokens). The ~29K full-mode prompt is
  mostly your `learning-corpus-context.sh` SessionStart hook + the harness briefing — NOT
  claude-mem.
- **Access pattern = auto-recall** (chosen): the model SEES memory automatically; it does
  not use the on-demand `mcp-search` tools (those + the corpus inject overflow 32K, and a
  local model is more reliable being handed memory than choosing to search for it).
  To enable search later, point full mode's `--mcp-config` at `~/backdoor/claude-mem-mcp.json`
  and trim the corpus inject for room.

### Auto-hook mode (the default)
Enables **claude-harness + claude-mem (memory) + ralph-loop (loop)** with their
hooks active — the real harness experience (auto-memory, session briefing,
checkpoints, loop). The other heavy plugins (superpowers/gstack/etc.) stay off
to keep the prompt at ~50K tokens. Config: `hook-mode.settings.json`.

> Note: do NOT rope-scale qwen2.5-coder above its native 32K — measured 260s vs
> 3.4s for the same tiny prompt at 64K vs 32K. Qwen3.5 is 256K-native, so the
> 128K tag has no such penalty.

### Overrides
- `QWEN_LEAN=1 qwen` / `qwen lean` — minimal prompt, no auto hooks.
- `QWEN_FULL=1 qwen` — force full mode (e.g. `QWEN_FULL=1 qwen coder`).
- `QWEN_MCP=0 qwen`  — drop global MCP servers (true-offline full mode).
- `QWEN_MCP=1 qwen lean` — force MCP on in lean/coder/fast modes.

## Performance reality (local Qwen3.5, full harness)

- First turn of a session: ~40s on the 9B (prefills the session-specific parts
  of the 50K prompt; the shared bulk is usually already in Ollama's KV cache).
  The 4B default has fewer layers/smaller KV, so prefill is lighter.
- Subsequent turns: ~8s on the 9B. Tool round-trips add ~2-5s each.
- `qwen lean`: ~6s/turn, full agentic Read loop ~16s.
- If turns suddenly take minutes again, suspect a new cache-busting prompt
  prefix (see proxy patch #3) — diff two consecutive payloads.

## Reliability notes

- A local model is not Opus. Expect more retries on complex agentic tasks; the harness
  helps, but keep tasks scoped tighter than you would on the cloud model.
- If a download dies and `ollama pull` later fails with `Error: EOF`, the multipart
  resume state is corrupt. Fix:
  `find ~/.ollama/models/blobs/ -name '*-partial-*' -size -1000k -delete` then re-pull.
- If responses look truncated or tool calls misbehave, check the loaded context:
  `ollama ps` should show CONTEXT 262144 for qwen3.5:4b-256k (the Modelfile
  num_ctx; 131072 for the -128k variant). The LaunchAgent's
  `OLLAMA_CONTEXT_LENGTH=32768` only applies to models without a baked num_ctx
  (i.e. the 32B/14B coders).
