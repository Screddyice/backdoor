# Local / offline LLM setup (Mac)

Run Claude Code — with your full claude-harness — against a **local, open-source
model**, completely offline. No internet needed once the models are downloaded.

```
Claude Code  ->  backdoor proxy (:8082)  ->  Ollama (:11434)  ->  local model
   (UI +              (translates              (serves the         (Qwen3.8 27B
    harness)           Anthropic<->OpenAI)      open weights)        on-device)
```

Everything runs on this Mac. The harness loads normally because it's a global
Claude Code plugin — the only thing that changes is the model underneath.

## One command

```bash
qwen            # Lean default on Qwen3.8 27B OBLITERATED at 32K.
qwen lean       # Same 27B lean route, stated explicitly.
qwen fast       # Lean mode on Qwen3.5 4B @ 64K (snappiest local brain).
qwen full       # Full harness on Qwen3.5 4B @ 64K.
```

(`claude-local` is a symlink alias for `qwen` if you prefer the longer name.)

The lean prompt makes the 27B practical inside its 32K window. The 4B keeps the
full-harness and long-context escape paths responsive. MCP servers attach per
request so their schemas do not consume the default context.

`qwen` makes sure the Ollama daemon is up, confirms the model is pulled,
points backdoor at the offline profile, and launches Claude Code. Anything after
the first word is passed straight to `claude` (e.g. `qwen fast --resume`).

## `/model qwen` in any terminal Claude Code session (hybrid router)

A second proxy instance runs permanently on **:8083** in `ROUTER_MODE=hybrid`
(LaunchAgent `com.screddy.backdoor-router`, KeepAlive). It routes by requested
model name: `qwen` / `qwen-fast` → the matching local profile;
**every other model and endpoint passes through byte-faithfully to
api.anthropic.com** (auth headers, SSE, compression untouched).

`~/.zshrc` exports `ANTHROPIC_BASE_URL=http://127.0.0.1:8083` for terminal
shells (health-guarded: if the router is down, new shells fall back to direct
Anthropic). So in any normal terminal Claude Code session:

    /model qwen        # switch this session to the local Qwen3.8 27B
    claude --model qwen -p "..."   # one-shot

**Cloud→local failover (2026-07-04):** if the real Anthropic API stops working
(network gone, usage limit hit, overloaded — 3 consecutive failures within
2 min), the router opens a circuit breaker and serves passthrough
`/v1/messages` traffic from a local model instead of failing, so the
in-flight session keeps going. It probes upstream every 60s and switches
back automatically; macOS notifications fire on both transitions. Auth
failures (401/403) stay visible on purpose — they mean a broken credential,
not a broken network.

**Size-aware tier (the ladder).** The local model is picked by the
failed-over session's estimated input tokens, so a big session keeps its
context instead of being truncated to fit the 4B:

| session input | tier | model | ~load @NUM_PARALLEL=4 |
|---|---|---|---|
| ≤ 27K | `local-qwen38-obliterated` | qwen3.8:27b-obliterated | ~17GB |
| > 27K | `local-failover-256k` | qwen3.5:4b-256k | ~13GB |

The router strips the harness before sizing. Most sessions fit the stronger
27B; the 4B 256K tag retains transcripts that outgrow its 32K window. Tune via env: `FAILOVER_TO_LOCAL=0`
disables; `FAILOVER_THRESHOLD` / `FAILOVER_PROBE_SECONDS` adjust; ladder
bounds live in `FAILOVER_LADDER` (config.py). Code: `src/proxy/failover.py`
+ routes + profiles `local-failover-{128k,256k}.env`.

Notes:
- The offline `qwen` wrapper still pins :8082 explicitly — unaffected.
- GUI/IDE/cron Claude Code sessions don't read .zshrc → by default they stay
  direct cloud (no failover, no /model qwen). Two ways to opt one in:
  - **cron / launchd / scripts:** `source ~/.local/bin/claude-router-env.sh`
    before `claude` (see "Non-terminal sessions" below). Same health guard as
    the .zshrc block — routes through :8083 when the router is up, direct
    Anthropic when it's down.
  - **GUI apps (Dock-launched IDE, Claude Desktop's local code sessions):**
    the `com.screddy.router-gui-env` LaunchAgent health-gates the GUI login
    domain's `ANTHROPIC_BASE_URL` (see "GUI apps" below). Relaunch the app once
    after install so it inherits the value.
- Do NOT put ANTHROPIC_BASE_URL in ~/.claude/settings.json env — settings env
  OVERRIDES process env (verified), which would hijack the :8082 wrapper AND
  remove the health-guard fallback from every session (router down → all
  sessions lose Anthropic). The sourceable helper keeps the guard; settings
  env can't.
- Restart: `launchctl kickstart -k gui/$(id -u)/com.screddy.backdoor-router`;
  log: `router.log` in the repo.

### Non-terminal sessions (cron / launchd / scripts)

Terminal shells get `:8083` from the health-guarded block in `~/.zshrc`.
Anything that doesn't read `.zshrc` (a cron job, a launchd plist, a helper
script running `claude -p`) opts in by sourcing the same guard:

```bash
source ~/.local/bin/claude-router-env.sh   # exports :8083 only if healthy
claude -p "…"                              # now has failover + /model qwen
```

`claude-router-env.sh` is a hand-placed helper (like the `qwen` wrapper — not
in this repo). It curls `:8083/health` with a 0.3s timeout and exports
`ANTHROPIC_BASE_URL=http://127.0.0.1:8083` only on success, so a job is never
worse off than direct cloud if the router is down. Nothing global changes, so
the terminal path's fallback is preserved. (There are no such Mac cron/launchd
`claude` jobs today — this is the ready-to-use path for when you add one.)

### GUI apps — Dock-launched IDEs and the Claude Desktop app

GUI apps inherit the launchd (Aqua) login environment, not `.zshrc`. The
`com.screddy.router-gui-env` LaunchAgent (`~/.local/bin/router-gui-env.sh`,
`RunAtLoad` + `StartInterval` 60s) health-gates that env:

```
router healthy → launchctl setenv   ANTHROPIC_BASE_URL http://127.0.0.1:8083
router down    → launchctl unsetenv ANTHROPIC_BASE_URL   (fallback to cloud)
```

This reaches the **local Claude Code sessions the Claude Desktop app hosts**
(the `cc*` / local-code-session features in `claude_desktop_config.json`) and
**Dock-launched IDE Claude extensions** — their `claude` engine runs as a
GUI-session child and inherits this var, so it gets cloud→local failover and
`/model qwen`.

Two boundaries to know:

- **GUI processes freeze their env at launch.** A running app won't see a
  toggle — **relaunch the app once** after installing the agent, and it inherits
  whatever the router state is at launch (held for the app's lifetime). This is
  why the terminal `.zshrc` block now also *unsets* on the down path: a terminal
  re-checks per shell, so it never holds a stale `:8083`.
- **The Claude Desktop app's OWN chat and Cowork are NOT redirected** — they
  talk to claude.ai's backend and ignore `ANTHROPIC_BASE_URL`. Only the *Claude
  Code* sessions the app hosts route through the router. There is no supported
  way to point the consumer assistant at a local model (and TLS-intercepting it
  would be a fragile, invasive hack — don't).

Blast radius is benign: any other GUI tool that reads `ANTHROPIC_BASE_URL` and
now hits `:8083` still works, because non-`qwen` models pass through
byte-faithfully to the real Anthropic API.

Install: `launchctl load -w ~/Library/LaunchAgents/com.screddy.router-gui-env.plist`
(plist + script are hand-placed, not in this repo). Log: `/tmp/router-gui-env.log`.

## Manual control (the underlying `bd` CLI)

```bash
bd list                 # show backends (* = active)
bd switch local-qwen35  # Qwen3.5 4B @ 64K (default)
bd switch local-fast    # same 4B, lean profile
bd switch modal-qwen    # back to the Modal cloud backend (needs internet)
bd claude               # launch Claude Code through the active backend
bd status               # what's active + proxy health
bd stop                 # stop the proxy
```

## Profiles

| Profile         | Model               | Notes                                    |
|-----------------|---------------------|------------------------------------------|
| `local-qwen35`  | qwen3.5:4b-64k      | 3.4GB weights. Tools+thinking. **Default.** |
| `local-fast`    | qwen3.5:4b-64k      | Same model; lean profile used by `qwen fast`. |
| `modal-qwen`    | (Modal cloud)       | Pre-existing cloud backend. Online only. |

The terminal `fusion-qwen` agent uses `qwen-fast` to frame verifiable tasks,
then runs the same separate verifier council as the cloud-framed Fusion agent.

The num_ctx is baked into the model (not `OLLAMA_CONTEXT_LENGTH`) so the global
32768 default stays modest for models without a baked num_ctx.
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
    big contexts stay comfortable on 36GB unified memory.
  - `OLLAMA_KEEP_ALIVE=5m` — unload the model 5 min after last use to free RAM/battery.
  - Restart it after edits: `launchctl kickstart -k gui/$(id -u)/com.screddy.ollama`
- **backdoor proxy** lives at `~/backdoor` (symlink to
  `~/projects/Screddyice/backdoor`). The `bd` CLI and `claude-local` wrapper
  are in `~/.local/bin`.
- **Profiles** are `profiles/local-qwen35.env`, `local-fast.env`.
  `PROVIDER_API_KEY=ollama` is a dummy (Ollama ignores it; bd just refuses an empty key).

## Updating / adding models (needs internet)

```bash
ollama pull qwen3.5:4b             # re-pull / update the default (then rerun modelfiles/build.sh)
ollama pull <some-other-model>     # then add a profile pointing PROVIDER_MODEL at it
ollama list                        # see what's available offline
```

## Proxy patches that make local models drive Claude Code

All in `src/proxy/`, committed to the Screddyice/backdoor repo:

1. **`translate.py` — content-embedded tool-call fallback.** Detects bare-JSON
   tool calls in message content (validated against the request's tool names)
   and converts them to real `tool_use` blocks, in both non-streaming and
   streaming paths. Qwen3.5 doesn't need this (native tool_calls); kept as
   generic robustness (originally for qwen2.5-coder, removed 2026-07-04).
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

## MCP servers attach per request

MCP tool schemas are huge — **~142K tokens** for this machine's global
`~/.claude.json` set, and even the curated stack overflows the 64K window. That
giant schema prefill was half of why full mode crawled, so **MCP is OFF by
default in all modes** (`--strict-mcp-config --mcp-config ~/backdoor/empty-mcp.json`),
which also keeps full mode truly offline.

Keep the default session lean, then attach only the server needed for a request:

```bash
qwen mcp list
qwen mcp screddy-hermes -p "check the requested conversation"
qwen mcp composio-tmn,atlassian
```

The wrapper validates names against `~/.claude.json` and enables the selected
servers only when its certificate-verifying internet probe succeeds. The
compact Cognee memory shim stays attached when Cognee is enabled. Missing
names fail before Qwen starts. An offline Mac skips the MCP connection and
continues with local tools. The environment form remains available for scripts:
`QWEN_MCP=1 QWEN_MCP_SERVERS=a,b qwen`.

## Full harness is the default (lean is the speed escape hatch)

On the 32B, prompt size made full mode unusable (minutes/turn), so lean was the
default. With the billing-header cache fix and MCP off, full mode on the 9B @ 64K
is ~40s first turn / ~8s warm turns — so the full harness (hooks, auto-memory,
briefing, loop) is the default.

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

> Note: do NOT rope-scale a model above its native context — measured 260s vs
> 3.4s for the same tiny prompt at 64K vs native 32K on the (since removed)
> qwen2.5-coder. Qwen3.5 is 256K-native, so its tags have no such penalty.

### Overrides
- `QWEN_LEAN=1 qwen` / `qwen lean` — minimal prompt, no auto hooks.
- `QWEN_FULL=1 qwen` — force full mode (e.g. `QWEN_FULL=1 qwen fast`).
- `QWEN_MCP=0 qwen`  — drop global MCP servers (true-offline full mode).
- `qwen mcp NAME lean` — attach one named MCP server in lean mode.

## Performance reality (local Qwen3.5, full harness)

- First turn of a session: ~40s (prefills the session-specific parts of the
  prompt; the shared bulk is usually already in Ollama's KV cache).
- Subsequent turns: ~8s. Tool round-trips add ~2-5s each.
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
  `ollama ps` should show CONTEXT 65536 for the qwen3.5 tags (the Modelfile
  num_ctx). The LaunchAgent's `OLLAMA_CONTEXT_LENGTH=32768` only applies to
  models without a baked num_ctx.

## Default system prompt (Claude Fable 5)

The local open-source models ship with a baked-in default system prompt:
the Claude Fable 5 prompt from the public `system_prompts_leaks` repo,
stored at `prompts/claude-fable-5-system.md` (43,112 tokens measured).

Coverage (one Modelfile per tag in `modelfiles/`):
- All six qwen3.5 tags: `4b`, `9b`, `4b-64k`, `9b-64k`, `9b-128k`, `9b-256k`.
- Persona variants of the council models: `llama3.1:8b-fable` and
  `gemma3:12b-fable` (num_ctx 65536 so the prompt fits).

Excluded, with reasons:
- The CANONICAL llm-jury council tags (`phi4`, `gemma3:12b`, `llama3.1:8b`)
  stay pristine: the jury sends bare user messages (no system role) at
  num_ctx 8192, so a baked 43K system prompt would overflow every council
  call and break fusion. Use the `-fable` variants for persona sessions.
- `phi4` (16K native) and `qwen3:8b` (41K max) cannot fit the prompt at all.

Scope:
- The baked prompt applies only when a request carries no system message of
  its own (bare `ollama run <tag>`, raw API calls).
- Claude Code sessions through the proxy send their own system prompt,
  which overrides the baked one. Harness behavior is unchanged (verified
  2026-07-04 with a request-level system message).
- Cold prefill of the prompt takes about 70s on the 4B, longer on the 8-12B
  variants; Ollama reuses the cached prefix while the model stays loaded.
- With `OLLAMA_NUM_PARALLEL=4`, loading a 64K-ctx 8-12B variant allocates
  heavy KV (4 x num_ctx) — treat the `-fable` variants as deliberate persona
  sessions, not background workers.

Rebuild after editing the prompt or a Modelfile:

```bash
modelfiles/build.sh                            # all tags
modelfiles/build.sh qwen3.5-4b-64k.Modelfile   # one tag
```

`ollama create -f <Modelfile>` alone builds WITHOUT the system prompt.
Re-pulling a base tag (`ollama pull qwen3.5:4b`) also overwrites the baked
prompt — rerun `build.sh` after pulls.
