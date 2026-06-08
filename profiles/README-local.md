# Local / offline LLM setup (Mac)

Run Claude Code — with your full claude-harness — against a **local, open-source
model**, completely offline. No internet needed once the models are downloaded.

```
Claude Code  ->  backdoor proxy (:8082)  ->  Ollama (:11434)  ->  local model
   (UI +              (translates              (serves the         (Qwen2.5-Coder,
    harness)           Anthropic<->OpenAI)      open weights)        on-device)
```

Everything runs on this Mac. The harness loads normally because it's a global
Claude Code plugin — the only thing that changes is the model underneath.

## One command

```bash
qwen            # Lean (default). 32B, ~945-tok prompt, ~6s/turn. Harness
                # slash-commands available on demand. FAST — daily driver.
qwen fast       # Lean on the 14B (snappier). OPTIONAL: needs `ollama pull qwen2.5-coder:14b`.
qwen full       # AUTO-HOOK mode: harness + claude-mem (memory) + ralph-loop,
                # hooks ACTIVE. ~29K-tok prompt => ~5 min cold first turn. Heavy.
```

(`claude-local` is a symlink alias for `qwen` if you prefer the longer name.)

**14B is optional.** Only the 32B is installed — it runs fine on this Mac even on
battery. If you ever want a lighter/faster model for unplugged use, install it once
while online and `qwen fast` will start working:

```bash
ollama pull qwen2.5-coder:14b
```

`qwen` makes sure the Ollama daemon is up, confirms the model is pulled,
points backdoor at the offline profile, and launches Claude Code. Anything after
the first word is passed straight to `claude` (e.g. `qwen fast --resume`).

## Manual control (the underlying `bd` CLI)

```bash
bd list                 # show backends (* = active)
bd switch local-coder   # 32B offline
bd switch local-fast    # 14B offline
bd switch modal-qwen    # back to the Modal cloud backend (needs internet)
bd claude               # launch Claude Code through the active backend
bd status               # what's active + proxy health
bd stop                 # stop the proxy
```

## Profiles

| Profile        | Model               | Notes                                   |
|----------------|---------------------|-----------------------------------------|
| `local-coder`  | qwen2.5-coder:32b   | ~20GB. Best local coding. Default.      |
| `local-fast`   | qwen2.5-coder:14b   | ~9GB. Faster, lighter — good on battery.|
| `modal-qwen`   | (Modal cloud)       | Pre-existing cloud backend. Online only.|

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
  `~/projects/third-party/ajsai47/backdoor`). The `bd` CLI and `claude-local` wrapper
  are in `~/.local/bin`.
- **Profiles** are `profiles/local-coder.env` and `profiles/local-fast.env` here.
  `PROVIDER_API_KEY=ollama` is a dummy (Ollama ignores it; bd just refuses an empty key).

## Updating / adding models (needs internet)

```bash
ollama pull qwen2.5-coder:32b      # re-pull / update
ollama pull <some-other-model>     # then add a profile pointing PROVIDER_MODEL at it
ollama list                        # see what's available offline
```

## Making qwen2.5-coder work inside Claude Code (proxy patches)

Two proxy changes were required so qwen2.5-coder actually drives Claude Code's
agentic loop (both in `src/proxy/`). These are local edits — re-apply if you ever
`git pull` the backdoor repo and they're lost:

1. **`translate.py` — content-embedded tool-call fallback.** qwen2.5-coder ignores
   the `<tool_call>` wrapper and emits the tool call as bare JSON in the message
   content, so Ollama never fills `tool_calls` and Claude Code sees plain text
   instead of a tool call. The proxy now detects that JSON (validated against the
   request's tool names) and converts it to a real `tool_use` block — in both the
   non-streaming and streaming paths. Streaming only buffers output that *looks
   like* a tool call (starts with `{`), so normal prose/code still streams live.
2. **`models.py` — accept non-user/assistant roles.** Claude Code sends `system`
   (and sometimes `tool`) roles inside the messages array; the proxy's strict
   `Literal["user","assistant"]` 422'd every request. `Message.role` is now `str`.

## MCP servers are OFF by default for local runs

`qwen` launches Claude Code with `--strict-mcp-config --mcp-config ~/backdoor/empty-mcp.json`.
Without this, this machine's global MCP servers load **~420 tools / ~148K tokens** —
4.5× a 32K-context local model, causing truncation and ~2 min/turn. MCP servers
also need internet, so they're useless offline. Disabling them leaves Claude
Code's built-in tools (Read/Write/Edit/Bash/Grep/Glob/…), which is what local
coding needs. Override with `QWEN_MCP=1 qwen` if you ever want them back.

## Lean mode is the default (this is what makes it usable)

The real bottleneck on a local model was **prompt size**, not the model. With all
plugins + hooks loaded, Claude Code's prompt is 30K+ tokens (skills lists, the
SessionStart briefing, etc.) → minutes/turn on a 32B. So `qwen` launches with:

    claude --bare --plugin-dir <claude-harness>  --strict-mcp-config ...

- `--bare` skips the heavy plugin/hook auto-injection → prompt drops to ~945 tokens.
- `--plugin-dir <harness>` re-adds the claude-harness plugin so its slash-commands
  (`/claude-harness:flow`, …) are available on demand — you just don't get the
  automatic hook behaviours (auto-memory, briefing injection).

Measured: **lean ≈ 945 tokens / ~6s per turn**; a full Read-tool agentic loop
completes in ~16s. Full mode ≈ 30K+ tokens / minutes per turn.

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

### Auto-hook mode (`qwen full`)
Enables **claude-harness + claude-mem (memory) + ralph-loop (loop)** with their
hooks active — the real harness experience (auto-memory, session briefing,
checkpoints, loop). The other heavy plugins (superpowers/gstack/etc.) stay off so
the prompt (~29K tokens) fits the native 32K window. Config: `hook-mode.settings.json`.

Trade-off: the cold first turn prefills ~29K tokens (~5 min on the 32B); only ~3K
headroom remains, so it's best for shorter memory-aware sessions, not long
file-heavy ones. **`qwen` (lean) is the daily driver; `qwen full` is opt-in.**

> Note: do NOT try an extended-context model variant. Qwen2.5-Coder above its
> native 32K (via Ollama rope-scaling) is catastrophically slow — measured 260s
> vs 3.4s for the same tiny prompt at 64K vs 32K. Stay at native 32K.

### Overrides
- `QWEN_FULL=1 qwen` — same as `qwen full`.
- `QWEN_MCP=1 qwen`  — keep your global MCP servers (adds ~420 tools; needs internet).

## Performance reality (local 32B)

- Generation is ~20 tok/s; lean mode keeps prefill tiny, so turns are ~6-16s.
- Ollama reuses the KV cache for a stable prompt prefix (verified: 39s cold → 0.3s
  warm), so repeated context within a session is cheap.
- For even snappier work, `ollama pull qwen2.5-coder:14b` and use `qwen fast`.

## Reliability notes

- A local model is not Opus. Expect more retries on complex agentic tasks; the harness
  helps, but keep tasks scoped tighter than you would on the cloud model.
- If a download dies and `ollama pull` later fails with `Error: EOF`, the multipart
  resume state is corrupt. Fix:
  `find ~/.ollama/models/blobs/ -name '*-partial-*' -size -1000k -delete` then re-pull.
- If responses look truncated or tool calls misbehave, check the context length is
  still 32k: `grep CONTEXT /tmp/ollama-launchd.log` (look for `default_num_ctx=32768`).
