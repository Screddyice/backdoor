# Local / offline LLM setup (Mac)

Run Claude Code — with your full claude-harness — against a **local, open-source
model**, completely offline. No internet needed once the models are downloaded.

```
Claude Code  ->  backdoor proxy (:8082)  ->  Ollama (:11434)  ->  local model
   (UI +              (translates              (serves the         (Qwen3.5 9B,
    harness)           Anthropic<->OpenAI)      open weights)        on-device)
```

Everything runs on this Mac. The harness loads normally because it's a global
Claude Code plugin — the only thing that changes is the model underneath.

## One command

```bash
qwen            # FULL harness (default). Qwen3.5 9B @ 64K ctx, MCP OFF, with
                # claude-harness + claude-mem + ralph-loop hooks ACTIVE.
                # (Fixed 2026-06-15: was 9b-256k, which loaded ~22GB and pinned
                # the Mac. 64K + MCP-off loads ~12GB and stays responsive.)
qwen lean       # Minimal (~945-tok) prompt on the 9B. Fastest; harness
                # slash-commands on demand, no auto hooks.
qwen fast       # Lean mode on the same Qwen3.5 4B @ 64K (snappiest local brain).
                # (qwen coder / qwen2.5-coder removed 2026-07-04.)
```

(`claude-local` is a symlink alias for `qwen` if you prefer the longer name.)

**Why Qwen3.5 9B @ 64K (MCP off):** Qwen3.5 is 256K-native (no rope penalty)
with native tool-calling (no content-JSON fallback) — which makes the FULL
harness viable. The full-harness prompt is ~30-50K tokens, so it needs real
context but NOT the 256K we ran before: at 256K the 9B's KV cache was ~16GB, the
model loaded ~22GB on the 36GB M5 Max, and it thrashed — every request crawled.
The fix (2026-06-15) is the same 9B at **64K** (~12GB loaded, comfortable) with
**MCP off** so the prompt stays inside the window. The 9B is the sweet spot on
36GB: Qwen3.5 jumps 9b → 27b, and a dense 27b is too heavy here.

`qwen` makes sure the Ollama daemon is up, confirms the model is pulled,
points backdoor at the offline profile, and launches Claude Code. Anything after
the first word is passed straight to `claude` (e.g. `qwen fast --resume`).

## `/model qwen` in any terminal Claude Code session (hybrid router)

A second proxy instance runs permanently on **:8083** in `ROUTER_MODE=hybrid`
(LaunchAgent `com.screddy.backdoor-router`, KeepAlive). It routes by requested
model name: `qwen` / `qwen-fast` / `qwen-9b` → the matching local profile;
**every other model and endpoint passes through byte-faithfully to
api.anthropic.com** (auth headers, SSE, compression untouched).

`~/.zshrc` exports `ANTHROPIC_BASE_URL=http://127.0.0.1:8083` for terminal
shells (health-guarded: if the router is down, new shells fall back to direct
Anthropic). So in any normal terminal Claude Code session:

    /model qwen        # switch this session to the local Qwen3.5
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
| ≤ 52K | `local-qwen35` | qwen3.5:4b-64k | ~7GB |
| ≤ 115K | `local-failover-128k` | qwen3.5:9b-128k | ~12GB |
| > 115K | `local-failover-256k` | qwen3.5:9b-256k | ~16GB |

The 9B tiers deliberately break the "harness = 4B" rule: during an outage a
big session kept ALIVE on a 9B beats one truncated to a 4B. They're safe on
36GB because `q8_0` KV + flash attention keep the KV small (the old 256K
"thrash" was f16 KV / the 142K MCP prompt, not this config), and Ollama
evicts idle models to make room. Cost: a 9B cold-prefilling a 100K+ session
takes minutes — slow but alive. Tune via env: `FAILOVER_TO_LOCAL=0`
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
| `local-qwen-9b` | qwen3.5:9b-64k      | Stronger brain for subagents (`qwen-9b` route); the `fusion` agent runs on it. ~10-12GB. |
| `modal-qwen`    | (Modal cloud)       | Pre-existing cloud backend. Online only. |

The `qwen-9b` route (→ `local-qwen-9b`) is a stronger local brain for
*subagents* that need more reasoning than the 4B — notably the **`fusion`
agent** (`~/.claude/agents/fusion.md`), which runs on `qwen-9b` to frame a
verifiable coding task and derive an oracle, then drives the llm-jury council
(`llmjury solve --backend ollama --frontier …`) to return a COUNCIL-VERIFIED
answer, escalating only the hard minority to a frontier model. The
full-harness default stays the 4B (`qwen`) per the "harness = 4B" rule; the 9B
is subagent-only. When the fusion agent then runs the council
(phi4+gemma3+llama), Ollama evicts idle models to fit and reloads the 9B when
the agent resumes.

`qwen3.5:9b-64k` is a local tag built from `modelfiles/qwen3.5-9b-64k.Modelfile`
(`FROM qwen3.5:9b` + `PARAMETER num_ctx 65536` — enough for the ~30-50K harness
prompt + conversation, while KV stays ~5GB so the model loads ~12GB). Rebuild
after re-pulling:

```bash
ollama pull qwen3.5:9b
ollama create qwen3.5:9b-64k -f ~/backdoor/modelfiles/qwen3.5-9b-64k.Modelfile
```

Bigger-context variants (`qwen3.5:9b-128k` / `-256k`) also exist for when you
want MCP on (the schemas don't fit 64K) — point `PROVIDER_MODEL` at one and
expect a heavier load. **Do not** make `-256k` the default again: at 256K the KV
cache is ~16GB and the model pinned the 36GB Mac (this was the slowness bug).

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

## MCP defaults: OFF in every mode

MCP tool schemas are huge — **~142K tokens** for this machine's global
`~/.claude.json` set, and even the curated stack overflows the 64K window. That
giant schema prefill was half of why full mode crawled, so **MCP is OFF by
default in all modes** (`--strict-mcp-config --mcp-config ~/backdoor/empty-mcp.json`),
which also keeps full mode truly offline.

Opt in with `QWEN_MCP=1` — but switch `PROVIDER_MODEL` to a bigger-context tag
first (`qwen3.5:9b-256k`), since the schemas don't fit 64K. `QWEN_MCP_SERVERS=a,b,c`
picks which servers (default: the curated stack — Composio TMN/Cliqk/TRC + NEBOS
+ HyperCrawl).

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
- `QWEN_MCP=1 qwen lean` — force MCP on in lean/fast modes.

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
