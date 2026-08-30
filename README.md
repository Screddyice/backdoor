```
██████╗  █████╗  ██████╗██╗  ██╗██████╗  ██████╗  ██████╗ ██████╗
██╔══██╗██╔══██╗██╔════╝██║ ██╔╝██╔══██╗██╔═══██╗██╔═══██╗██╔══██╗
██████╔╝███████║██║     █████╔╝ ██║  ██║██║   ██║██║   ██║██████╔╝
██╔══██╗██╔══██║██║     ██╔═██╗ ██║  ██║██║   ██║██║   ██║██╔══██╗
██████╔╝██║  ██║╚██████╗██║  ██╗██████╔╝╚██████╔╝╚██████╔╝██║  ██║
╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚═════╝  ╚═════╝  ╚═════╝ ╚═╝  ╚═╝
```

<div align="center">

### Claude Code is the best coding agent ever built. The model underneath is optional.

**Backdoor lets you run Claude Code against any AI — DeepSeek, Groq, Ollama, OpenRouter, or your own local model. Same UI. Same tools. Same agentic loops. Zero lock-in.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Works with Claude Code](https://img.shields.io/badge/works%20with-Claude%20Code-blue)](https://docs.anthropic.com/en/docs/claude-code)
[![Providers](https://img.shields.io/badge/providers-any%20OpenAI--compatible-orange)](README.md)

</div>

---

## `/model Qwen` and the RAM interlock

Model names are matched case-insensitively. `MODEL_ROUTES.get(model)` was a
plain dict lookup, so `/model Qwen` missed and the session silently stayed on
the cloud model -- the one failure mode nothing reports, because a passthrough
is a legitimate outcome. `qwen`, `Qwen`, `QWEN` and surrounding whitespace all
resolve to the same tier now, as do the other local names.

**Engaging the MLX Qwen3.8-27B tier evicts every resident Ollama model first.**
That tier holds roughly 17 GB and an llm-jury council holds roughly 21 GB; this
host has 36 GB with a wired ceiling near 27, so the two cannot co-reside.
Getting it wrong has kernel-panicked this Mac twice, and nothing enforced it --
`mlx_admin` carried a comment saying it was "bounded by `qwen38 stop`", which is
a person remembering, not a guard.

The eviction is logged at warning level, never silent: a jury run losing its
council mid-flight should be explainable from the log rather than looking like a
crash. It is a no-op when Ollama holds nothing, and a failure to evict is logged
and the request proceeds -- refusing to answer would trade a memory risk for a
certain outage.

**Scope.** The guard fires when the tier is engaged THROUGH THE ROUTER. Starting
the MLX server by hand with `qwen38 start` and separately loading a council
still collides; the router is not in that path.

## The problem

Claude Code is genuinely in a league of its own as a coding agent. The tool use, the agentic loops, the way it navigates a codebase — nothing else comes close.

But it's hardwired to Anthropic's API. You pay Anthropic's prices. You use Anthropic's models. You play by Anthropic's rules.

That's a lot of lock-in for a tool that could theoretically run on anything.

---

## The fix

Backdoor is a lightweight proxy that intercepts Claude Code's API calls and reroutes them to any OpenAI-compatible provider. It translates the request format, streams the response back, handles tool use — everything. Claude Code has no idea anything changed.

```
Before:   Claude Code  →  Anthropic API  →  $$$
After:    Claude Code  →  Backdoor  →  literally anything
```

Three commands to set up. One line to switch providers.

---

## The numbers don't lie

We ran **500 million tokens** through Claude Code. Same workload. Two different backends.

| | Claude Opus 4.7 | DeepSeek V3 Flash |
|---|---|---|
| **500M tokens** | **$9,834** | **$5.34** |
| Cost per 1M tokens | ~$19.67 | ~$0.01 |
| **Savings** | — | **99.95% cheaper** |

That's not a typo. **$9,834 vs $5.34. Nearly 2,000x cheaper at scale.**

If you're an individual developer, that's the difference between a credit card bill that hurts and one that rounds to zero. If you're a team running Claude Code across multiple engineers, this is the difference between a budget line item that gets cut and one nobody notices.

The harness is free. The intelligence is cheap. That's the whole point.

### Measure it for your own usage

`scripts/claude-savings-report.py` measures what the local router and prompt caching saved. It deliberately does NOT count llm-jury or its OpenRouter ladder: that is a separate system, on a separate provider, running different models, with its own billing. The script used to read `~/.llmjury/.env` for an API key and call OpenRouter to fold that spend in. Backdoor has no business holding another system's key, and a report mixing the two answers neither question cleanly.

It turns the numbers above into a weekly report on *your*
traffic. It reads Claude Code's own transcript logs (`~/.claude/projects/**/*.jsonl`), separates
turns actually routed through Backdoor (local Ollama, OpenRouter) from turns that went straight
to Claude, and reports $ saved against what that same work would have cost at metered API
pricing.

Prompt caching is tracked as a separate efficiency stat, never counted as dollars saved — on a
flat-rate subscription plan there's no per-token bill for it to discount off of.

```bash
python3 scripts/claude-savings-report.py --days 7   # print a report
python3 scripts/claude-savings-report.py --dry-run   # preview, writes and emails nothing
```

Optional weekly email delivery goes through Gmail via Composio (`SAVINGS_EMAIL_TO`,
`SAVINGS_EMAIL_FROM_ACCOUNT`); pass `--no-email` to skip it. Every counterfactual — the pricing
model, the subscription cost, the plan's usage band — is a tunable env var documented in the
script's own header.

---

## What you actually get

🆓 **Run it free.** NVIDIA NIM and Groq both have free tiers with thousands of requests per month. Backdoor works with both out of the box.

💸 **Run it cheap.** DeepSeek costs roughly **95% less** than Claude's API. If you're using Claude Code daily, that's the difference between a coffee and a car payment.

🔒 **Run it private.** Point Backdoor at a local Ollama instance and nothing ever leaves your machine. No API. No logs. No data sent anywhere.

🔀 **Run whatever model you want.** One config change and you're on a completely different AI. Benchmark Llama vs. Mistral vs. DeepSeek against your actual work, using the best coding agent UI ever built as the harness.

🌍 **200+ models, one tool.** Wire up OpenRouter and you have access to every major model on earth — Gemini, Grok, Qwen, Command R, all of them — through a single API key.

---

## Get running in 60 seconds

```bash
git clone https://github.com/ajsai47/backdoor
cd backdoor
./backdoor   # animated setup wizard — picks your provider, writes your config, launches Claude Code
```

That's it. The wizard handles everything: animated intro, provider selection, API key entry, and launching your first session. If you'd rather skip the wizard and configure manually, copy `.env.example` to `.env`, fill it in, and run `./run.sh` directly.

---

## Pick your provider

Edit three lines in `.env` and you're on a different AI:

```bash
PROVIDER_BASE_URL=https://api.deepseek.com/v1
PROVIDER_API_KEY=your-key
PROVIDER_MODEL=deepseek-chat
```

| Provider | Free tier? | Speed | Best for |
|---|---|---|---|
| **DeepSeek** | — | Fast | Best value. Insanely cheap. |
| **Groq** | ✅ | Fastest | When you want responses instantly |
| **NVIDIA NIM** | ✅ | Fast | Free Llama 3.3 70B |
| **OpenRouter** | — | Varies | Access to everything, one key |
| **Ollama** | ✅ (local) | Depends on your machine | Total privacy, no internet needed |
| **LM Studio** | ✅ (local) | Depends on your machine | Easy local model management |

---

## Why this exists

I'm a huge fan of Anthropic. Genuinely. Their models are some of the most impressive technology I've ever used, and Claude Code is the best coding agent ever built — it's not close.

This project isn't a knock on them. It's a tribute to how good the harness is.

What I believe is simple: the best tools should be accessible to everyone, not just the people who can afford the top-tier API. A student, a solo developer, a builder in a country where $9,000/month in API costs is unthinkable — they deserve to experience what Claude Code can do. Vendor lock-in shouldn't be the thing standing between a great developer and a great tool.

Anthropic built the magic. Backdoor makes sure everyone gets to use it.

**Run Claude Code. Bring your own model. Keep building.**

---

## Open source. No strings.

MIT licensed. Read the code — it's clean, it's simple, it's less than 600 lines. Fork it, change it, build on it.

If a new AI provider launches tomorrow, you can use it with Backdoor the same day. No waiting for a pull request. Just drop in the URL.

---


## Bonus: control Claude Code from your phone

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_ID` in `.env` and you can trigger Claude Code sessions from Telegram. Send a prompt from your phone, get the output back in chat.

---

## Running the tests

Run them through the project venv, not the `pytest` on your PATH:

```bash
.venv/bin/python -m pytest -q
```

A bare `pytest` picks up whichever interpreter comes first on PATH, and on a machine with Homebrew Python that one has none of this project's dependencies installed. It fails during collection on `fastapi` and `httpx` imports, which reads like broken tests rather than the wrong interpreter. Same suite, same machine, both ways:

```
pytest                          5 errors during collection
.venv/bin/python -m pytest      54 passed
```

The harness verification gate in `.claude-harness/config.json` runs the venv form for the same reason.

### Tests never touch the network

Connectivity is injected, never observed. `internet_reachable` is exercised by patching
`socket.create_connection`, and the failover breaker takes an `online_fn`, so the suite runs
identically on a plane and in an office.

This is a rule rather than a preference because breaking it produced a test that failed on
correct code. The offline case used to assert that a connection to TEST-NET-1 (`192.0.2.1`, which
RFC 5737 reserves and promises is unroutable) would be refused. That promise binds the public
internet, not the machine running the suite — and it is the worst possible thing to lean on here,
because *a network that answers TCP on every address is the exact failure mode this probe exists
to detect*. Some networks, including one this repo was developed on, run a transparent middlebox
that completes a connection to any address in ~0.2s. TEST-NET included. So the test failed on
precisely the networks the code most needs to be right about.

That same middlebox used to defeat `internet_reachable` itself, which was the more serious half of
the problem. See below.

### The probe verifies who answered

`internet_reachable` opens a TCP connection to `1.1.1.1:443` and `8.8.8.8:443`, **completes a TLS
handshake, and verifies the certificate chain and hostname**. Only that counts as online.

Until 2026-08-17 a completed TCP connection was enough, and that was a silent hole. A transparent
middlebox accepts a connection to any address, so the probe reported "online" with the internet
gone, the breaker never opened, and **failover silently did not happen** — the one situation the
mechanism exists for. A box that answers TCP has nothing the trust store accepts for
`one.one.one.one` or `dns.google`, so it cannot fake the verified handshake.

Both middlebox flavours are handled, and they fail differently: one speaks TLS with an untrusted
certificate, the other accepts the socket and says nothing until the timeout. Either way the probe
logs a warning naming the address, because "no network" and "something is answering for the entire
internet" need different responses from you, and moves on to the next probe rather than giving up —
interception is per-route, so a hijacked path to `1.1.1.1` does not make a clean `8.8.8.8` unusable.

Still literal IPs, and the hostname is only ever used as SNI and verified — never resolved — so a
broken resolver still cannot fold itself into the answer.

One case this cannot catch: a corporate MITM proxy whose CA is installed in this machine's trust
store. Such a proxy is generally forwarding traffic, so "online" is the right answer anyway.

| Setting | Default | Effect |
| --- | --- | --- |
| `BACKDOOR_PROBE_TCP_ONLY` | unset | Set to `1` to fall back to the pre-2026-08-17 TCP-only probe |

The escape hatch exists because a false *offline* is not a harmless failure here — it claims the
GPU and routes a live session to a local model while the cloud was fine. If verification ever
misbehaves on a network this was not tested against, it can be switched off without a deploy.
Leaving it set re-opens the hole.

## Troubleshooting

**`500 ... "system message must be at the beginning"`**
Ollama 0.32 rejects any system message at index > 0, including a payload that already opens with a valid one. Claude Code does send `system` roles inside the messages array, and Ollama 0.23.4 accepted them anywhere, so this appears the moment the daemon is upgraded and hits every tool-using session. The proxy normalises this now (`_hoist_system_messages`); if you see it again, the running service is on older code than you think — check which checkout it is actually serving:

```
plutil -p ~/Library/LaunchAgents/com.screddy.backdoor-router.plist | grep WorkingDirectory
```

**Every Claude session shows "Waiting for API response - check your network"**
The proxy is down, and one likely reason used to be that it could not start at all. Check first:

```
lsof -iTCP:8084 -sTCP:LISTEN -P
curl -sk -m 6 --proxy http://127.0.0.1:8084 https://api.anthropic.com/v1/models -o /dev/null -w "%{http_code}\n"
```

A `401` in under a second is healthy: that is the correct unauthenticated answer, and it proves
the whole path works. If nothing listens on 8084, read `~/Library/Logs/backdoor-router.log` for
`backdoor failed during startup`.

Until 2026-08-26 that message usually meant tiktoken. `src/proxy/tokens.py` built its encoder at
import time, so starting the proxy required downloading `cl100k_base.tiktoken` from
`openaipublic.blob.core.windows.net`. tiktoken caches to a temp directory macOS prunes, so the
download returned on its own schedule, and a DNS failure while the Mac woke or a VPN settled
raised inside `import` - before uvicorn could load the app. launchd relaunched into the same
failure. Because every terminal Claude session on the machine proxies through :8084, all of them
sat in retry backoff at once. On 2026-08-26 that cost seven hours: two failed starts at 08:10 and
no proxy until 15:10.

The encoder now loads on first use instead of at import, caches under `~/.backdoor/tiktoken` so it
survives temp pruning, and falls back to a characters-per-token estimate if it cannot be built.
Token counts only pick a local-model failover profile, so an estimate costs precision at a routing
threshold and nothing else. Override the cache location with `TIKTOKEN_CACHE_DIR`.

A restart still severs requests already in flight. Those sessions recover on their next retry, or
at once with Esc and resend.

**The router is serving code you did not just edit**
The `:8083` router runs from a *separate* deploy checkout (`backdoor-service`), in detached HEAD, not from your dev clone. Editing the dev clone changes nothing until you deploy:

```
cd ~/projects/Screddyice/backdoor-service && git fetch origin && git checkout <sha>
launchctl kickstart -k gui/$(id -u)/com.screddy.backdoor-router
```

A dev-clone process started by hand on the same port hides this completely, because it answers
first and serves current code. Kill it and the stale service takes over, which reads as a sudden
unexplained regression. Confirm which one is live with `pgrep -fl "src.proxy.serve"` and compare
its interpreter path against the LaunchAgent's.



**`API error · Retrying…` banners in Claude Code, nothing in the router log**
An upstream transport failure below the failover threshold used to return a bare 502 with no
trace, so intermittent banners could not be correlated with anything. Every transport failure
now logs `upstream transport failure (<type>)` at WARNING — grep the router log for that
before suspecting the proxy stack. The usual culprit is the network path underneath: on
2026-08-20 a VPN on a distant exit pushed connection setup past the old 10s connect limit
572 times in one evening. New upstream connections now get 30s to establish — the same
patience Claude Code shows when talking to Anthropic directly — so a slow path degrades to
slower turns instead of visible errors.

**`Proxy failed to start`**
Something is already using port 8082. Either stop the other process or change `PORT=8083` in your `.env`.

**`PROVIDER_API_KEY is not set`**
Open `.env` and replace `your-api-key-here` with your actual key.

**`Claude Code is not installed`**
Install it from [claude.ai/code](https://claude.ai/code).

**`Model not found` or `404` from the provider**
The model name in `PROVIDER_MODEL` doesn't match what the provider expects. Check the provider's docs for the exact model slug — they vary (e.g. DeepSeek uses `deepseek-chat`, Groq uses `llama-3.3-70b-versatile`).

**Responses feel slow**
Switch to Groq — it's the fastest inference available and has a free tier.

**Local model on a small CPU box takes minutes per turn**
This is expected, and no amount of config fixes it. A coding-agent harness sends a large prompt every turn (tool schemas, system prompt, context files — commonly 12-17k tokens), and CPU-only prefill is the bottleneck.

Measured on a 2-vCPU / 8 GB cloud VM with no GPU: **~6.4-7.0 tok/s prefill**, which is ~30 minutes before the model emits its first token, plus ~2.8 tok/s generation. A 3.4B model and a 4.7B model both landed there; a bigger model is strictly worse, since prefill scales with parameter count.

Things that do *not* rescue it: raising `num_ctx`, keeping the model resident, disabling thinking mode, or trimming the prompt (12,800 tokens at 7 tok/s is still ~30 minutes). Doubling vCPUs only halves it.

Two caveats worth knowing before you benchmark your own box:

- **Short test prompts lie.** A ~375-token prompt can report 116-141 tok/s because it's largely a cache hit. Read Ollama's `prompt processing` log lines on a *full-size* prompt instead of timing a toy request.
- **Watch RAM against your context window.** A 3.4B model with a 32k KV cache left only 185 MB free on a 7.9 GB box. 16k was the practical ceiling there.

If you want a local model for interactive agent work, budget for a GPU. For CPU-only boxes, a hosted provider is the realistic answer.

**Nothing is logged**
Check `proxy.log` in the project directory. If it's empty, the proxy didn't start at all — run `uv run uvicorn server:app` directly to see the error.

---

## How it works under the hood

Claude Code talks to `localhost:8082` thinking it's Anthropic. Backdoor receives the request, translates it from Anthropic's Messages API format to OpenAI's chat completions format, forwards it to your chosen provider, and streams the response back — translated back into Anthropic's SSE format in real time. Tool calls, streaming deltas, token counts — all handled transparently.

A handful of Claude Code's internal housekeeping requests (quota probes, title generation, etc.) are intercepted and short-circuited locally so they don't burn your provider quota.

---

## Codex cloud-to-local failover

Codex uses a different protocol from Claude Code. Its ChatGPT-backed sessions post to the Responses API, so the Anthropic `/v1/messages` router cannot catch a failed Codex turn. Backdoor exposes a separate Responses relay at `http://127.0.0.1:8083/backend-api/codex`.

Add this to `~/.codex/config.toml` while keeping your existing ChatGPT login:

```toml
model_provider = "backdoor"

[model_providers.backdoor]
name = "Backdoor"
base_url = "http://127.0.0.1:8083/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
supports_standalone_web_search = false
```

Restart Codex Desktop after changing the shared configuration. Dock-launched processes do not reload terminal configuration or a modified TOML file mid-session.

Leave `model_context_window` and `model_auto_compact_token_limit` unset. Codex uses the selected cloud model's catalog limits. Backdoor applies Qwen's smaller window after it chooses local failover and rebuilds the request.

### What happens to a running thread

While ChatGPT inference works, Backdoor relays the original request, OAuth headers, response status, and SSE bytes. Backdoor sends cloud traffic without parsing it or applying Qwen's 32K check. ChatGPT enforces the selected cloud model's input limit. Codex compaction requests also stay on the ChatGPT relay. Codex sends account, plugin, and other hosted traffic to ChatGPT because Backdoor replaces the inference provider alone.

The relay accepts request bodies up to 64 MiB by default. This is a transport safety ceiling, not a model token limit. Set `CODEX_MAX_REQUEST_BYTES` only when a client must send a larger encoded request.

After three eligible failures inside 120 seconds, the Codex breaker routes the turn to `qwen3.8:27b-obliterated` through Ollama. Transport failures and `429,500,502,503,504,529` responses count. HTTP `400`, `401`, and `403` never count, so a malformed request or broken login remains visible instead of being hidden by Qwen.

The visible Codex thread does not change. Qwen receives a fresh internal request containing:

- the latest user instruction and the active local tool loop, including a paired tool continuation that does not repeat the user message;
- a bounded recall from Cognee when `QWEN_COGNEE` is enabled;
- local Code Mode tools converted from Codex's Responses Lite namespace format.

It does not receive the old cloud transcript, cloud reasoning context, prompt-cache identifiers, OAuth headers, remote MCP schemas, hosted web-search tools, or image and file attachments. Cognee can provide continuity without forcing the 27B model to prefill the session that caused the outage or compaction failure. Backdoor reads agent recall through `POST /api/v1/recall`. Durable fetched-source storage remains off unless an operator configures reviewed public URL prefixes; authenticated, browser-session, malformed, and unpaired tool results remain ephemeral. Set `QWEN_COGNEE=0` to suppress every Cognee read and write on the local path.

Cognee authentication resolves from the running process, `~/.cognee/.env`, then the existing `~/.cognee-plugin/api_key.json` cache when its server URL matches. The key is not copied into the Backdoor LaunchAgent.

The breaker permits one ChatGPT probe every 60 seconds while open. A probe closes it the moment ChatGPT returns response headers, the same point the Anthropic path uses: a status line proves this host can still reach the upstream, and reachability is the only question the breaker asks. Later turns return to cloud immediately. Qwen is released separately, once a relayed stream finishes, because a probe whose body dies mid-flight is about to be retried and unloading the tier that will serve that retry only buys a reload.

Crediting the probe at the end of the stream instead looks equivalent and is not, because the failure is silent. On 2026-08-30 a blip opened the Codex breaker at 23:09:48. Probes at 23:13:46 and 23:15:23 both reached ChatGPT and logged `path=cloud status=200`. The breaker stayed open for 19 minutes anyway, answering two Codex turns from the local 27B in 309s and 385s while the cloud was fine. Both clients hung up before their bodies finished, and closing a generator raises `GeneratorExit` at the `yield`, which runs neither the transport-error branch nor the success branch. A probe that plainly worked was discarded twice. A client that hangs up before the first chunk never starts the generator at all, so no handling inside the body can see that probe succeed.

Before Codex receives a local response, Backdoor removes Qwen reasoning items from streaming SSE and non-streaming JSON. It reindexes retained SSE output and removes every local `encrypted_content` field. Ollama places plaintext reasoning in that field, while ChatGPT expects an opaque value that it issued and can verify. Letting a local reasoning item enter the thread makes the first recovered cloud turn fail with `invalid_encrypted_content`. Tool calls and visible assistant output remain in the thread; healthy cloud responses still pass through byte-for-byte.

Backdoor caps each local SSE frame and non-streaming JSON body at 8 MiB. It requests uncompressed local responses and rejects any encoded response before reading its body, which prevents decompression from bypassing those limits. Invalid or oversized JSON returns `502 Local Qwen returned an invalid response`. An invalid or oversized SSE frame closes the local stream and releases its runtime slot.

### The local 32K allocation

Backdoor enforces this allocation on the fresh request that it sends to Qwen:

| Component | Limit |
| --- | ---: |
| Local guidance | 1,000 tokens |
| Cognee recall | 2,000 tokens |
| Local tool schemas | 4,000 tokens |
| Current task and active tool results | 21,000 tokens |
| Reply reserve | 4,000 tokens |

Backdoor removes extra recall, optional tools, old tool output, and attachments in that order. It never truncates the latest textual instruction. If that instruction cannot fit, Codex receives HTTP 413 with a clear error.

Set `CODEX_LOCAL_TOOLS=` to remove all local tools. The default `local` keeps non-MCP Code Mode tools. Remote `mcp__*` namespaces stay out of the outage request because they cannot work without the network.

Set `CODEX_FAILOVER_TO_LOCAL=false` to disable Qwen fallback without removing the custom provider. Cloud inference still crosses the Responses relay and returns its real errors. Backdoor logs correlation IDs, route choice, status class, timing, cloud request byte counts, local token counts, recall counts, and tool counts. It never logs prompts, recalled text, OAuth values, tool arguments, or model output.

---

## Offline failover (hybrid mode)

In hybrid mode Backdoor passes Anthropic-bound traffic straight through to the real API and only steps in when it has to. A circuit breaker watches passthrough requests, and when it opens, `/v1/messages` is served by a local Ollama profile instead — so an in-flight session survives losing the network. The profile is chosen by session size, so a large context escalates to a wider-window tier rather than being truncated.

> **Put Backdoor in the request path, or none of this runs.** Failover lives in the request path, so a session that reaches api.anthropic.com directly gets a plain API error when the network drops. That is not a bug in the breaker; the breaker was never consulted. If an outage produced an error instead of a local answer, check the routing first — the two supported ways to be in the path are `ANTHROPIC_BASE_URL` and the forward proxy below.

### Every passthrough route feeds the breaker

Four handlers forward to Anthropic: `/v1/messages`, `/v1/messages/count_tokens`, the `/{path:path}` catch-all, and `/v1/messages` again when you set `failover_to_local=false`. All four report a transport failure to the breaker, and none of them let one escape as an unhandled exception.

Until 2026-08-24 only the first did. The other three called the passthrough with no `except` around it, so a `ConnectTimeout` propagated out of the handler, uvicorn dropped the client socket, and Claude Code printed `Connection dropped (ECONNRESET) · Retrying`. Retrying hit the same unguarded handler, so the banner climbed to attempt 8 of 10 while the router log filled with tracebacks rather than the single line naming the failure.

Losing the count was the expensive half. `count_tokens` runs on nearly every turn, so during an outage it produced most of the evidence that Anthropic was unreachable, and every bit of it was raised and thrown away. The breaker saw a fraction of the failures, which pushed it past `failover_window_seconds` before it reached `failover_threshold`.

What each route does now when upstream will not answer:

| Route | Response |
|---|---|
| `/v1/messages` (failover on) | Local profile once the breaker opens, `502` below the threshold |
| `/v1/messages` (failover off) | `502` |
| `/v1/messages/count_tokens` | Counts from the request body. Arithmetic needs no model, no tier and no GPU, so this one answers through any outage |
| `/{path:path}` | `502` |

Recording a failure never opens the breaker on its own. `internet_reachable()` still decides that, and these routes never call `record_success`: closing the breaker obliges the caller to unload the tiers it claimed, and only the `/v1/messages` path knows how.

### A stream that dies after the headers

The guard above stops at the headers. Once Anthropic answers `200` and Backdoor starts relaying the body, that response is committed: your client is already reading it, and no local model can take over a turn halfway through.

Two incidents on 2026-08-26, at 23:17 and 23:47, both surfaced in Claude Code as `The response stopped arriving. The response above may be incomplete.` The router log said nothing about either one. Its last word on both turns was the `→ passthrough` line that started them.

`aiter_raw()` went into `StreamingResponse` bare, so a transport error inside the body escaped to uvicorn, which answers mid-response by dropping the client socket. The breaker never heard about it, which mattered on the retry: your client comes back within seconds, and a breaker that counted nothing has no reason to serve that retry locally.

Backdoor cannot rescue the truncated request. It now does the two things still available to it:

| | |
|---|---|
| Logs it | `upstream stream died mid-response after N byte(s)`, with the exception name and how far the body got |
| Counts it | `record_failure()`, so the retry can be served locally instead of truncating again |

A dead stream still runs the connectivity probe before anything opens. If Anthropic dropped your stream while this host is online, Backdoor relays the failure and leaves the GPU alone.

Hanging up yourself does not count. `CancelledError` and `GeneratorExit` are not `httpx.TransportError`, so pressing Ctrl-C never pushes the breaker toward claiming the GPU.


### Forward-proxy mode: failover *and* Remote Control

Setting `ANTHROPIC_BASE_URL` to the router costs you Claude Code's Remote Control. Claude Code only offers it when that variable is unset or points at `api.anthropic.com`:

```js
function eit(){ let e=process.env.ANTHROPIC_BASE_URL; if(!e) return true; return Oxe(e) }
function Oxe(e){ let t=new URL(e).host; return ["api.anthropic.com"].includes(t) }
```

The allowlist is exact and there is no escape hatch — the binary states that `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL` "does not apply to Remote Control". That forced an either/or: routed sessions had failover but no phone control, direct sessions had phone control but no failover.

The premise was wrong. Backdoor has to be **in the path**, not **named as the endpoint** — and Claude Code honours `HTTPS_PROXY`. Enable `FORWARD_PROXY` and the router also serves an HTTP CONNECT proxy on `:8084`:

```
claude  (ANTHROPIC_BASE_URL unset  →  Remote Control offered)
  │  HTTPS_PROXY=http://127.0.0.1:8084
  ├── CONNECT api.anthropic.com:443 ──► TLS-terminated ──► :8083 router
  └── CONNECT everything else       ──► opaque tunnel, untouched
```

Point a session at it:

```bash
env -u ANTHROPIC_BASE_URL \
    HTTPS_PROXY=http://127.0.0.1:8084 \
    NODE_EXTRA_CA_CERTS=~/.backdoor/ca/ca-cert.pem \
    claude
```

`/model qwen` and offline failover keep working, and `claude remote-control` starts instead of refusing.

#### Wiring it without touching the environment

Claude Code applies its own `env` block to every session, so the proxy can live in `~/.claude/settings.json` instead of a shell profile:

```json
{
  "env": {
    "HTTPS_PROXY": "http://127.0.0.1:8084",
    "NODE_EXTRA_CA_CERTS": "/Users/you/.backdoor/ca/ca-cert.pem",
    "NO_PROXY": "localhost,127.0.0.1,::1,github.com,.github.com,githubusercontent.com,.githubusercontent.com",
    "no_proxy": "localhost,127.0.0.1,::1,github.com,.github.com,githubusercontent.com,.githubusercontent.com"
  }
}
```

**Exempt GitHub.** Every command a session runs inherits that `env`, so `git` and `gh` tunnel through `:8084` as well. They gain nothing from it, and they pay for the extra hop: a dropped tunnel surfaces as `CONNECT tunnel failed` from `git push`, or `POST https://api.github.com/graphql: Bad Gateway` from `gh pr merge`. A 502 there is ambiguous, because a merge that fails that way may still have landed. The `NO_PROXY` entries above route GitHub straight out. Both cases are listed because curl reads the lowercase name first, and `githubusercontent.com` is a separate apex that covers release assets, LFS objects, and raw fetches.

For `git` specifically, a per-URL override holds even when the environment does not:

```bash
git config --global http.https://github.com.proxy ""
```

This is usually what you want, for two reasons. It reaches GUI surfaces — the Claude Desktop app and IDE extensions never source your shell profile, so an exported variable never gets to them. And it stays scoped to Claude Code, unlike a login-wide `launchctl setenv HTTPS_PROXY`, which would put this proxy in front of every other application's HTTPS traffic.

Two behaviours are worth knowing before you rely on it:

- **A settings `env` value beats an empty process variable.** Clearing `HTTPS_PROXY` in the shell does *not* opt a session out; settings.json fills it back in.
- **`NO_PROXY=*` is the opt-out.** It bypasses the proxy for every host, which is what a "go direct" launcher wants. Useful for a health-gated wrapper: if `:8084` is unreachable, export `NO_PROXY=*` so the session degrades to a plain cloud session instead of failing every request against a dead proxy.

Settings `env` has no health gate of its own, so pair it with a wrapper that checks the port when you care about that degradation path.

**Backdoor retries one transient Anthropic transport failure before surfacing it.** Connect,
write, read, and protocol failures can clear between attempts. The router retries once on the
same pool, then lets the circuit breaker decide whether the host is offline. Pool exhaustion uses
a fresh pool as described below.

**Backdoor recovers the Anthropic pool after a dropped network.** Active streams can occupy every
shared HTTP connection while Wi-Fi changes or DNS disappears. A later request then raises
`PoolTimeout` even after direct access to Anthropic recovers. Backdoor now replaces that exhausted
pool, closes it, and retries the request once. Check `router.log` for
`Anthropic connection pool exhausted; rotated pool and retrying once`. A second timeout still
reaches Claude Code so its normal retry and failover behavior stays intact.

**Give the macOS service enough file descriptors.** A CONNECT proxy holds one client socket and
one upstream socket for each tunnel. Intercepted Anthropic traffic also crosses the loopback
router. The launchd default of 256 files can run out when several Claude sessions start together,
which makes new streams fail with `ECONNRESET` and writes `OSError: [Errno 24] Too many open files`
to the router log. The example LaunchAgent at
`deploy/com.screddy.backdoor-router.plist.example` sets `NumberOfFiles` to 4096. Existing jobs
must copy that `SoftResourceLimits` block, switch `ProgramArguments` to
`python -m src.proxy.serve`, and move the old uvicorn host and port flags into the `HOST` and
`PORT` environment keys shown in the example. The startup-safe launcher creates bounded logging
before uvicorn imports the application. Replace every `/Users/you` placeholder before installing
the file. `kickstart` does not reload a changed plist. Boot out the loaded definition, bootstrap
the file, then confirm the applied limit:

```bash
launchctl bootout --wait gui/$(id -u)/com.screddy.backdoor-router
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.screddy.backdoor-router.plist"
launchctl print gui/$(id -u)/com.screddy.backdoor-router | grep -A3 resource-limits
```

The example writes application logs to `~/Library/Logs/backdoor-router.log`. Backdoor rotates
that file at 10 MiB and keeps three backups; launchd output goes to `/dev/null` so it cannot
create a second, unbounded copy under `/tmp`.

**Only allowlisted hosts are inspected.** `FORWARD_MITM_HOSTS` defaults to `api.anthropic.com`; everything else — Composio, npm, your MCP servers, and the Remote Control bridge to claude.ai — is relayed as opaque bytes, so nothing else can have its TLS broken by a proxy it never asked for.

**About the CA.** Reading a CONNECT tunnel means presenting a certificate for a host you do not own, so Backdoor mints one from a CA at `~/.backdoor/ca` (key `0600`). It is **never** installed into the system keychain: the only thing that trusts it is a process you hand `NODE_EXTRA_CA_CERTS` to. Every other program on the machine rejects anything it signs. Delete the directory to revoke it; it regenerates on next use.

| Setting | Default | Purpose |
|---|---|---|
| `FORWARD_PROXY` | `false` | Enable the CONNECT proxy |
| `FORWARD_PORT` | `8084` | Port it listens on |
| `FORWARD_MITM_HOSTS` | `api.anthropic.com` | Hosts to intercept; all others tunnel blind |
| `FORWARD_ROUTER_PORT` | `8083` | Where intercepted traffic is delivered |
| `FORWARD_IDLE_TIMEOUT` | `660` | Close a tunnel after both directions remain byte-idle this many seconds |
| `FORWARD_MAX_CONNECTIONS` | `512` | Reject excess tunnels before they can consume upstream descriptors |
| `FORWARD_CA_DIR` | `~/.backdoor/ca` | CA and minted leaves |

Set `FORWARD_ROUTER_PORT` explicitly if you launch uvicorn with `--port`: that flag never reaches `Settings`, so `PORT` will not reflect it.

#### Do not run the service from your dev checkout

Once a launchd job or systemd unit points `WorkingDirectory` at the clone you develop in, the service inherits whatever branch you happen to have checked out. Check out a branch that predates `src/proxy/forward.py` and the next restart silently drops the forward proxy.

It fails quietly by design. `app.py` imports `forward` and `ca` inside the lifespan, wrapped in `try/except`, so the router keeps serving `:8083` rather than taking down inference for every session on the machine:

```
Forward proxy failed to start; continuing without it
```

The router looks healthy. `:8084` never listens. Any session pointed at it by `settings.json` now fails every request against a dead port, and a GUI surface has no shell wrapper to fall back on.

Give the service its own checkout. A detached worktree pinned to `main` keeps your dev tree free to sit on any branch:

```bash
git worktree add --detach ../backdoor-service main
cd ../backdoor-service
uv sync --frozen
ln -s ../backdoor/.env .env                                 # gitignored, loaded relative to cwd
ln -s ../../backdoor/profiles/modal-qwen.env profiles/      # gitignored profile, if you use it
```

Detached rather than a checked-out branch, so `git checkout main` still works in the dev tree. Symlink the config instead of copying it, so the two never drift apart. Then point the service at `backdoor-service` and deploy on purpose:

```bash
git -C ../backdoor-service fetch origin main
git -C ../backdoor-service checkout --detach origin/main
(cd ../backdoor-service && uv sync --frozen)
launchctl kickstart -k gui/$(id -u)/com.screddy.backdoor-router
```

The tradeoff is that editing code in your clone no longer deploys. That is the point: a restart
can no longer pick up half-finished work. The example LaunchAgent writes the rotating router log
to `~/Library/Logs/backdoor-router.log`.

One launchd wrinkle if you script that swap: `launchctl bootout` returns before teardown finishes, so an immediate `bootstrap` fails with `Bootstrap failed: 5: Input/output error`. Poll `launchctl print gui/$(id -u)/<label>` until it errors, then bootstrap.

### Bare mode: what the local model actually receives

A failed-over request does not reach the local model as Claude Code sent it. Backdoor strips the harness off first, and this is the change that decides which model you can afford to run.

Measure an ordinary session and the reason becomes obvious. With the usual MCP servers attached, the system prompt and tool definitions alone came to roughly 286K tokens on this machine, before a word of conversation. Two model generations of this failover ladder were shrunk to cope with that number: the tier went 9B down to 4B in July 2025 after a 9B spent about five minutes per turn prefilling a 186K-token session and then returned a 500. The context was treated as fixed and the model kept getting smaller.

Bare mode attacks the other term. Before the request goes to Ollama, Backdoor drops the system prompt (replacing it with two sentences of orientation), drops tool definitions, truncates tool results in the transcript to a character budget, and replaces images with a placeholder. Your conversation stays. On a representative request that arrived carrying a full harness and an 80KB file read:

```
50,439 tokens  ->  1,196 tokens        (42x)
answer in 3.6s on qwen3.5:27b, ending in a real tool call
```

That is what lets the default tier be a 27B rather than a 4B without repeating the prefill regression. The two changes belong together: switch bare mode off and leave the 27B in place, and you rebuild the original failure on a much larger model.

**Local tools survive; MCP tools do not.** `Read`, `Edit`, `Bash`, `Glob` and `Grep` touch nothing but this disk, so they keep working while the host is offline, and keeping them means the failover model can carry on doing work instead of only talking about it. Every `mcp__*` tool is a remote integration and is dead for exactly as long as the breaker is open. That split also happens to be where the weight is: the ~286K tokens of definitions came from MCP servers, not from the dozen local tools, so dropping them removes nearly all of the cost and nearly none of the offline capability.

Mem0 sits on the dropped side and loses nothing. Its MCP tools call `mcp.mem0.ai` and cannot work offline, but local Mem0 recall still reaches the model, because the recall hook reads `~/.mem0-local/cache.db` client-side and injects memories into the prompt before the request leaves the machine. Bare mode keeps that text.

**The tier must accept tool definitions.** This is a hard pairing, not a preference. `deepseek-r1` at any runnable size does not: Ollama answers a request carrying tools with `does not support tools`, HTTP 400, killing the session failover exists to save. If you swap the tier for a model without tool support, set `failover_keep_tools=""` at the same time.

### Deliberate routing: `/model qwen`

Failover is not the only way into a local tier. A session can ask for one by name with `/model qwen`, and that request takes a different branch. It matches `MODEL_ROUTES`, returns a profile immediately, and skips the failover block underneath. Only the failover block stripped.

So `/model qwen` handed the 27B a full harness session against a 32K window, which is the pairing the section above warns about. That window is small on purpose. Bare mode is what makes it generous.

Profiles whose window assumes bare mode now say so:

```
ROUTE_BARE=true
```

Set it only on those. The 64K tiers stay untouched, and one of them has to: `qwen-9b` backs the `fusion-qwen` subagent, which needs its system prompt and its tools to do anything at all. Stripping every named route would break that quietly.

| `/model` name | Profile | Model | Window | Stripped |
|---|---|---|---|---|
| `qwen` | `local-qwen38-obliterated` | Qwen3.8-27B OBLITERATED Q4_K_M (GGUF) | 32K | yes |
| `qwen38-obliterated` | `local-qwen38-obliterated` | the same tier, named directly | 32K | yes |
| `qwen38-action` | `local-qwen38-action` | action-tuned MLX rollback | 64K | yes |
| `qwen-stock` | `local-failover-heavy` | `qwen3.5:9b-64k` | 64K | yes |
| `qwen-fast` | `local-fast` | `qwen3.5:4b-64k` | 64K | no |
| `qwen-9b` | `local-qwen-9b` | `qwen3.5:9b-64k` | 64K | no |

Stripping reuses the `failover_*` keep-list and truncation budget, so both paths build the same request shape. A route that stripped differently from failover would be a second behaviour to keep in sync for no gain.

#### Stripping bounds the prompt, not the transcript

`ROUTE_BARE` fixes the *prompt* and leaves the *conversation* alone, and the conversation is the half that grows. A long-lived `qwen` session therefore walks past its own window with nothing to stop it — and because `MODEL_ROUTES` is a static dict, it never consults `FAILOVER_LADDER`, which is the one place that would have handed it to a wider tier.

Observed 2026-08-12: a `qwen` session sent **143,490 tokens at the 27B's 32K window, 87 times over ~17 hours**, failing and retrying every 5–10 minutes and loading 23GB of a 36GB host on every attempt. The window was configured correctly. There was simply no route from "too big for this tier" to "use the wider one".

Tiers now declare the largest post-strip session they will take:

```
ROUTE_MAX_INPUT_TOKENS=27000
```

Set it to the same bound the tier carries in `FAILOVER_LADDER`, so a deliberate route and a failover size that tier identically. Over it, the request escalates through the same ladder rather than failing:

```
⇢ ROUTE ESCALATE [local-qwen38-obliterated → local-failover-256k] in≈143490 over 27000
```

Sizing happens **after** stripping, matching the failover branch — size the raw body and a bare-able session escalates to the wide 4B tier for no reason, wasting the stronger model. `0` disables the check, which is what the unstripped 64K tiers use.

#### The client has to know the window too

Tier escalation is a reaction. It catches a session that has already outgrown its tier and finds it somewhere wider to land. Nothing in it stops the transcript growing in the first place, and the growth has a cause on the client side.

Claude Code does not recognise the model name `qwen`, so it falls back to assuming a 200K window and sizes auto-compact against that number. The 27B serves 32K. Compaction therefore sat idle through roughly six times the context the model could accept, which is the same 143,490-token session the escalation guard was built to catch, viewed from the other end. The router saw a request too large for its tier. The client saw a session comfortably inside a window that did not exist.

The wrapper now states the real window before launching:

```
CLAUDE_CODE_MAX_CONTEXT_TOKENS=32000   # local-failover-heavy
CLAUDE_CODE_MAX_CONTEXT_TOKENS=32000   # local-qwen38-obliterated
CLAUDE_CODE_MAX_CONTEXT_TOKENS=64000   # local-qwen35, local-fast
```

Keep the value equal to the profile's actual `num_ctx`. Setting it above the true window restores the original bug in a quieter form, because compaction again waits for a ceiling the model cannot reach. An unknown profile falls back to 32000, the floor, on the principle that compacting early costs a little quality and compacting late costs the session. An explicit `CLAUDE_CODE_MAX_CONTEXT_TOKENS` in the environment still wins, for deliberate experiments.

The two guards are complements, not alternatives. Compaction keeps ordinary sessions inside the tier; escalation catches the ones that jump anyway, such as a single oversized paste. The obliterated tier also caps generation at 4,096 tokens, so its 27K input ceiling leaves room for output and template overhead inside the 32,768-token runtime window.

Claude Code 2.1.250 also subtracts its output reservation before calculating the auto-compact threshold. With `xhigh` effort and an unrecognized model name, it reserved 20K from Qwen's declared 32K window. That left a 12K effective input window. The client's lean plugin and tool prefix could exceed 12K while Backdoor's stripped request contained only about 3K tokens, so a two-message session started compacting.

The `qwen` wrapper now sets `CLAUDE_CODE_MAX_OUTPUT_TOKENS=4096`, matching the provider cap. Claude Code then gives input 27,904 tokens, which lines up with the route's 27K escalation guard. The wrapper scopes this setting to local sessions, so cloud Claude sessions keep their normal output allowance. It also passes `--model qwen`; a saved Opus setting can no longer make the client calculate a 1M window while the wrapper serves the 32K local model.

Every launcher needs its own client policy because Claude Code and Codex calculate compaction before Backdoor sees the request:

| Entry path | Client policy |
|---|---|
| `qwen` | declares the selected profile window, caps output at 4,096, and pins `--model qwen` |
| `bd claude` on a Qwen profile | derives the profile window, reads `PROVIDER_MAX_TOKENS`, and pins `--model qwen` |
| `claude --model qwen` | the shell launcher applies the 4,096 output cap for an explicit Qwen start |
| `/model qwen` inside a routed Claude session | unknown-model enforcement stays disabled for that process; Backdoor strips the request and escalates past 27K instead of letting Claude Code compact against a false 12K window |
| Codex through Backdoor | keeps the selected cloud model's catalog window; Backdoor rebuilds local failover requests within Qwen's 28K input budget |

The mid-session Claude path cannot change its process environment after `/model` runs. Its router guard supplies the hard boundary. Known Claude model IDs keep their native compaction policy, and cloud output remains uncapped.

The backend must also return usable summary text. On 2026-08-28 the action-tuned MLX tier reached Claude Code's client limit at 32K. The compact request itself was small: 994 backend tokens. MLX generated nine tokens, all inside its inline thinking block. `PROVIDER_STRIP_INLINE_THINKING=true` removed those tokens and Claude Code received an empty summary twice. The default route now uses the OBLITERATED Q4_K_M GGUF. Its OpenAI endpoint returned an ordinary answer alongside internal reasoning in live compaction testing. The MLX checkpoint remains available as `qwen38-action` for measured action-contract work.

#### Memory is the other half of a small window

A short window is only workable if the facts have somewhere else to live. `QWEN_COGNEE` therefore defaults to **1** (flipped from opt-in on 2026-08-22), attaching Cognee memory over the two-tool stdio shim.

This is the one documented exception to the MCP-off rule, and the token arithmetic is why it survives that rule. The global MCP set costs about 142K tokens of schema. The shim exposes `cognee_search` and `cognee_remember` and nothing else, so it costs hundreds. Against a 32K window the first is impossible and the second is affordable.

Without memory, every durable fact has to be carried in-context, which is precisely what fills the window that the section above just finished bounding. Both failure modes have the same shape, so both fixes ship together.

Large fetched pages also bypass the model window at the proxy layer, making the behavior independent of GUI plugins. Once Claude, Codex, or another client returns a page as a tool result, Backdoor replaces results over 12,000 characters with up to 6,000 characters of passages ranked against the current question. Durable storage is off by default. `EXTERNAL_CONTEXT_PUBLIC_URL_PREFIXES` accepts comma-separated, reviewed public URL prefixes whose unauthenticated fetch output may be submitted with its URL and content hash to the dedicated `qwen_external_context` Cognee dataset. Browser-session tools never persist their output. Later Qwen turns search that dataset and inject only a bounded set of recalled passages. The first turn does not wait for Cognee indexing: local ranking supplies its excerpts while Cognee processes an approved durable copy in the background.

This boundary is client-independent because every local Qwen request crosses Backdoor. Backdoor does not fetch arbitrary URLs itself; the client remains responsible for browsing and authentication. Unapproved, authenticated, intranet, and client sources remain ephemeral even when their content looks harmless. Approved source text is marked as untrusted data, high-confidence credential-shaped documents are not written, and stored source URLs exclude user information, query strings, and fragments. Individual documents are capped at 500,000 characters before ranking, and each request can enqueue at most four documents. Cognee calls time out after 1.5 seconds. A Cognee or SSH-tunnel failure never drops the request. `QWEN_COGNEE=0` disables writes and recall for true-offline work, while the local 6,000-character reduction still protects Qwen's window.

The proxy resolves Cognee settings from its process environment, then `~/.cognee/.env`, then `~/projects/.env`. That makes the same behavior available to terminal launchers and Dock-launched clients that never source `.zshrc`. A missing key or unreachable service degrades to local reduction only.

#### The guard has to sit below every routing branch

The paragraph above describes the check on the **hybrid** path, and on its own it does not cover the incident it cites. That pile-up came from a `qwen` wrapper session on `:8082`, which runs `router_mode="profile"` — a mode that translates every request to the single active profile and never enters the hybrid branch at all. A guard living inside that branch cannot see the traffic that actually failed.

So the tier check runs **after every routing branch has chosen**, where no path can skip it:

```
⇢ TIER ESCALATE [qwen3.8:27b-obliterated → qwen3.5:4b-256k] in≈143490 over 27000
```

Three properties make that placement safe:

- **It escalates by model, not by profile name.** However the tier was selected — hybrid route, failover ladder, or a plain profile translation — a request is never bounced to a profile serving the model it is already on.
- **It cannot fire twice.** The wide tiers leave `ROUTE_MAX_INPUT_TOKENS` unset, so once the hybrid branch has escalated, the backstop finds nothing to do.
- **Escalation failure is not request failure.** If the wider profile cannot be loaded, the request continues on its original tier and surfaces an honest provider error, which is what it did before.

The runtime interlock follows the same placement rule. Profiles that manage a large runtime set `RUNTIME_PROFILE`, and the router resolves it after both routing branches and size escalation. That keeps the hybrid `/model qwen` path and the wrapper's profile-mode path under the same MLX/Ollama exclusion guard.

The general lesson: `router_mode` is a real fork in this file, and a guard is only as good as the branch it sits in. Verify a fix against the mode the failure actually used, not the one you were reading when you wrote it.

Making the 27B the deliberate default also keeps it resident far more often, which feeds straight into the arithmetic in the next section. This Qwen3.8 GGUF measures **17GB** resident and a fusion council is roughly 21GB. They do not both fit under this host's wired-memory ceiling, and Ollama caps by model count, so nothing upstream refuses the combination.

### Keeping the 27B warm without starving the council

The wrapper warms its tier at launch so the first turn skips the cold load, then holds it with `keep_alive`. That hold is shorter on the 27B, and the reason is the 44GB in the paragraph above:

| Profile | `keep_alive` |
|---|---|
| `local-qwen38-obliterated` | **10m** |
| `local-failover-heavy` | **10m** |
| `local-qwen38-action` | n/a, not an Ollama tag |
| every other profile | 30m |

Thirty idle minutes of a resident 27B is thirty minutes in which `llmjury solve` can collide with it, and neither side will refuse: Ollama counts models, not bytes. Ten minutes still spans an active session and returns the GPU sooner. Failover has a stronger interlock and does not need this — llm-jury reads `~/.backdoor/failover-state.json` and stands down while the breaker is open — but a deliberate `qwen` session writes no such file, so the shorter hold is the only thing bounding the overlap.

The warm-up call **must** carry a system message. A systemless request falls through to the baked ~46K-token Fable-5 SYSTEM prompt on the `*-64k/128k/256k` tags and cold-prefills all of it, which takes about a minute and looks exactly like an offline hang.

The `qwen` wrapper reaches Ollama by a third path and never reads this table. It runs the proxy in `profile` mode on :8082, where every request translates to the active profile and nothing strips server-side. Its modes pick their own tier:

| Command | Profile | Model | Why |
|---|---|---|---|
| `qwen`, `qwen lean` | `local-qwen38-obliterated` | Qwen3.8-27B OBLITERATED Q4_K_M (GGUF) | `--bare` keeps the prompt small and the OpenAI endpoint returns textual compaction output |
| `qwen full` | `local-qwen35` | `qwen3.5:4b-64k` | the harness runs about 29K tokens and needs the wider window |
| `qwen fast` | `local-fast` | `qwen3.5:4b-64k` | the escape hatch when the heavy tier costs more GPU than the task is worth |

The wrapper prints the tier it resolved at launch. Read that line if you are unsure which model you got.

#### What `--bare` takes away, and what lean mode puts back

Client-side `--bare` is what keeps the lean prompt near 945 tokens, but it is blunter than the name suggests. Per `claude --help` it skips *"hooks, LSP, plugin sync, attribution, auto-memory, background prefetches, keychain reads, and CLAUDE.md auto-discovery"*. Two of those matter more than the token saving:

- **Hooks do not run.** Not reduced — off. A `SessionStart` probe fired without `--bare` and stayed silent with it, and `--settings` does not put it back.
- **CLAUDE.md is not loaded.** The session gets none of the repo or machine conventions it would normally start with.

Together those quietly removed pull requests from local-model sessions. The convention that every branch gets a PR lives in CLAUDE.md, so the model never saw it; the hook that opens the PR anyway is a hook, so it never ran. Branches accumulated commits, no PR appeared, and nothing reported a failure — the two mechanisms that would each have caught the other were disabled by the same flag.

Lean mode now restores both, narrowly:

- `--append-system-prompt` injects `prompts/qwen-pr-rules.md` (~600 tokens): branch naming, open the draft PR on the first commit, update the README, check `gh repo set-default` when the repo has an `upstream` remote, and leave RS21 alone. Loading real CLAUDE.md files with `--add-dir` would also work, but at ~33KB it would spend a quarter of the 27B's window on context that is mostly irrelevant to a coding turn.
- The wrapper runs `auto-pr-push.sh` after the session exits, which is why it no longer `exec`s. That is the same script the Stop hook would have run; it no-ops unless the branch is off trunk, has commits ahead, has no open PR, and belongs to an allowlisted owner, and it refuses any repo with `rs21` in the name.

If you write your own wrapper around `--bare`, assume nothing in `.claude/` applies to that session.

### Releasing the failover tier when the outage ends

Ollama's only release mechanism is one global `OLLAMA_KEEP_ALIVE` (5m here), refreshed by every request. That is the wrong shape for a tier nobody asked for, and it fails worst exactly when it matters most: because each request pushes the timer out, a *busy* outage releases the GPU later than a quiet one.

Measured 2026-08-24 on this host. A ten-minute Anthropic blip opened the breaker at 22:09:31. Seven sessions were live, all long — 242K to 431K tokens post-strip — so every one cleared the ladder's 28K bound and landed on the same `local-failover-256k`:

| | |
|---|---|
| Breaker open | 22:09:31 → 22:24:41 (15m 10s) |
| Requests served locally | 138, peaking at 21/min |
| GPU allocated | 13.71 GB (9.69 GB in use), 99% utilization |
| Wired memory | 13.9 GB of 36 GB |
| Free memory | 25%, with swap at 14.3 GB of 15.36 GB |
| Tier still resident after close | **~9 minutes** |

Nothing was wrong with the routing. The ladder picked correctly and the breaker closed on time. What was missing was the wiring between "breaker closed" and "tier released", so 9.7 GB of wired memory sat on the machine for nine minutes after the last thing that needed it.

Two guards now, because either alone leaves a hole:

| Guard | Mechanism | Covers |
|---|---|---|
| Unload on close | `record_success()` drains the breaker's claims; the caller `POST`s `keep_alive: 0` | The normal case, precisely |
| `PROVIDER_KEEP_ALIVE` | Failover-only profiles clamp idle residency to `45s` | An outage that ends with sessions abandoned and no successful call to close the breaker |
| Wait for live streams | The unload defers while any failover response is still generating | A breaker that closes while a slow local prefill is mid-answer |

Both go through Ollama's **native** API. `keep_alive` in a `/v1/chat/completions` body is silently ignored (verified against Ollama 0.32.13) and the model lands on the global default, so the clamp has to be a separate `/api/generate` call. With no `prompt` that call neither generates nor prefills — it returns `done_reason: "load"`, or `"unload"` for `keep_alive: 0`, and only touches the residency timer.

Closing the breaker does not mean the tier is idle. The breaker closes on the first upstream success, and that success is a newer request than the failover streams still running. A local tier prefilling a 386K-token session emits nothing for minutes, so a stream dispatched during the outage is often still open when the outage ends.

On 2026-08-26 a failover stream opened at 23:10:34. The breaker closed at 23:14:17 and released `qwen3.5:4b-256k` inside the same 62ms window, while that stream was still generating. It produced nothing after that and died on the 600-second read timeout at 23:20:38.

Backdoor now counts the failover responses that are still generating and holds the unload until the last one finishes. If a fresh outage re-opens the breaker while an unload waits, it drops the unload: that tier is claimed again, and releasing it would evict a model the new outage is already serving from.

Recovery is not instant. While OPEN the breaker probes upstream once per `failover_probe_seconds` (60s), so it cannot notice Anthropic is back until it is allowed to try. Release lands within about a minute of real recovery, against the nine minutes above.

`PROVIDER_KEEP_ALIVE` is set on `local-failover-256k` and `local-failover-128k` only. Do not set it on a tier reachable through `MODEL_ROUTES`: a deliberate `/model qwen` session that thinks for longer than the clamp would evict its own 17 GB model and reload it next turn, which is slower and more memory churn than leaving it resident. The same rule is why only breaker-diverted requests are claimed at all — the user asked for that tier, so it is not ours to evict.

Everything here is best-effort. A router that cannot reach Ollama's admin endpoint must still route, and the cost of failing is late release, which is the old behaviour rather than an outage.

### Sizing the failover tier

Ollama caps residency by model count, never by bytes, and Metal allocations are wired and cannot be paged out. Over-commit and this host panics instead of raising OOM. Measure with `ollama ps`, which reports resident size; `ollama list` reports on-disk size and will mislead you by roughly half.

On a 36GB M5 Max at `OLLAMA_NUM_PARALLEL=2`, flash attention on, `q8_0` KV cache:

| Tier | Params | On disk | Resident | Tools | Notes |
|---|---|---|---|---|---|
| `qwen3.5:9b-64k` | 9B | 6.6 GB | ~10-12 GB | yes | Default heavy tier since 2026-08-25 |
| `qwen3.8:27b-bare` | 27B | 17.7 GB | **17 GB** | yes | Removed 2026-08-25 to free disk. Modelfile kept; see below |
| `qwen3.5:27b-bare` | 27.8B | 17.4 GB | 23 GB | yes | Predecessor, removed 2026-08-16 |
| `qwen3.5:4b-256k` | 4B | 3.4 GB | ~13 GB | yes | Escape hatch for a transcript that overflows 32K |
| `deepseek-r1:14b` | 14.8B | 9.0 GB | 20 GB | **no** | Rejected. Larger footprint than the 27B and cannot call tools |

**3.8 costs 6 GB less resident than the 3.5 tag it replaces**, which is not the direction anyone predicted: 3.8 carries an *extra* vision projector layer, and the estimate written down before measuring was 24 GB. The guess missed by 7 GB. Measure, do not compute.

Verify a swap with both of these, not just the first:

```
ollama run qwen3.8:27b-bare "hi" >/dev/null && ollama ps   # resident + CONTEXT (must read 32768)
# then re-run ollama ps after a few thousand tokens of context
```

The second check is the one that catches the old MLX failure: that build sat at a lazily-allocated ~15 GB floor and grew toward 32 GB as a session filled its window. A GGUF load allocates KV up front, so a resident number that does not move under load is the proof the window is enforced. 3.8 held at 17 GB after an 8K-token fill.

Measure, do not compute. The first estimate for the 14B was 16GB and the real number was 20GB, because the arithmetic omitted the compute graph. `ollama ps` reports resident size; `ollama list` reports on-disk size and will mislead you by roughly half.

### The default local brain is `qwen38-obliterated`

The default `qwen` route now runs `OBLITERATUS/Qwen3.8-27B-OBLITERATED` Q4_K_M through Ollama's GGUF engine. It uses the same standalone path as the earlier stock `qwen3.8:27b-bare` tier: a 32,768-token context clamp, bare client prompt, and no separately managed MLX server. The source model card reports 82.3% MMLU against stock's 84.5%, 20/20 tested cyber and code tasks, and 7/8 advanced agent tasks. Those are publisher measurements, not local verification.

Install and build the local tag:

```bash
ollama pull hf.co/OBLITERATUS/Qwen3.8-27B-OBLITERATED:Q4_K_M
ollama create qwen3.8:27b-obliterated \
  -f modelfiles/bare/qwen3.8-27b-obliterated.Modelfile
```

The bundled GGUF template does not advertise tools, so Ollama rejects Claude Code requests with `does not support tools`. The Modelfile installs the tool-capable template from the stock local `qwen3:8b` tag, clamps `num_ctx` to 32,768, and sets the publisher's required `repeat_penalty` to 1.15. Live checks must cover both tool-call serialization and a non-empty compact response before this tag is used as the default.

The 27B GGUF and the 27B MLX server cannot share memory safely. Before the router serves `local-qwen38-obliterated`, `mlx_admin` stops either managed MLX profile and waits for port 8080 to go quiet. If the server cannot stop, the router selects `local-fast` and logs the collision instead of loading both 27B runtimes.

### The action-tuned rollback is `qwen38-action`

Qwen3.8-27B Action-Abliterated comes from `ajsai47/qwen38-action-abliterated-research`: a pinned Qwen3.8-27B checkpoint trained on action contracts, then put through a bounded refusal-direction ablation. Its model card records a 92.5% HarmBench direct-request attack-success rate, and StrongREJECT assistance on forbidden prompts at 87.22% against the base model's 10.54%. Capability held flat, with 62.50% on a frozen 280-item MMLU-Pro sample, matching upstream.

From 2026-08-25 through 2026-08-28 it backed `/model qwen`, the wrapper's lean mode, and cloud-to-local failover. The empty compaction response moved those unattended paths to the GGUF tier. `qwen38-action` still names this checkpoint directly. `qwen-stock` routes to the 9B when you want a model whose refusal behaviour is intact.

Read the model card before you lean on it. Reduced refusal is not permission, and it says so itself: the card puts unsupervised execution with destructive, financial, credential, or otherwise high-impact tools out of scope, and this wiring puts the model on exactly those paths. Failover fires with nobody watching, and `local-worker` gets dispatched with Bash and Write. Scoped tool permissions and reading what an unattended agent actually did are the controls now, because the model is no longer one of them.

#### It is the one tier nothing loads lazily

Every other local tier is an Ollama tag that loads on first request and gets evicted on a timer. This one is a launchd job holding about 19GB, up or absent, with nothing in between. Pointing failover at a tier that cannot start itself would break the fallback in the one situation it exists for, so `src/proxy/mlx_admin.py` probes `127.0.0.1:8080/health`, runs `launchctl kickstart` when it finds nothing, and waits up to 90 seconds for the weights to load. When the server will not come up, the request goes to `local-failover-heavy` and the log says so:

```
⇢ MLX FALLBACK [local-qwen38-action → local-failover-heavy] /v1/messages
```

That fallback is why `local-failover-heavy` still exists. Deleting it because `qwen` points elsewhere now would leave an offline host with no answer at all.

Ollama cannot evict this server either, so `qwen38 stop` before an `llmjury solve` run rather than letting a 19GB server and a 21GB council fight over a 36GB host.

#### Setup

```
hf auth login
HF_HUB_DISABLE_XET=1 hf download ajs-ai/Qwen3.8-27B-Action-Abliterated-MLX-4bit
uv tool install mlx-vlm --with jinja2
local/install-qwen38.sh
```

Two traps live in that download. `hf-xet` 1.6.0 stalls at zero bytes instead of running slow, so turn it off. `HTTPS_PROXY` also points at the backdoor forward proxy on :8084, and its `NO_PROXY` covers github.com but not huggingface.co, which drags a 16GB pull through mitmproxy at about 1 MB/s. The install script verifies the download against the `SOURCE_SHA256SUMS` the artifact ships with and refuses to pin a snapshot that fails. `README.md` is the one tolerated mismatch, because Hugging Face prepends model-card frontmatter on publish and the manifest predates it. `jinja2` needs `--with` because mlx-vlm does not depend on it, yet `apply_chat_template` calls it: without it the server passes `/health` and fails every completion.

Manual control, for when you want it:

```
qwen38 start     # 65,536 context, 8-bit KV, ~19GB resident
qwen38 status
qwen38 stop
```

#### Thinking traces

This tier leaves `<think>` tags inline in `content` instead of filling a `reasoning` field, and Qwen's chat template pre-fills the opening tag, so the stream starts inside the block. Left alone, a `qwen` turn renders the model's reasoning, a bare `</think>`, and then the real answer.

`PROVIDER_STRIP_INLINE_THINKING=true` in the profile re-homes that into an Anthropic thinking block, on both the streaming and non-streaming paths. Both are gated on it, and the gate is the point: ungated, an answer that legitimately contains the literal characters, such as "to close a thinking block you write `</think>`", would have everything before the tag silently reclassified as reasoning. It is opt-in per profile because knowing the stream begins inside a block is what makes it free: deltas route straight to a thinking block. Detecting it generically would mean buffering every stream until a closer appeared, delaying first paint on every tier to fix one.

`PROVIDER_REASONING_EFFORT=none` does not help here. That is an Ollama-ism, and `mlx_vlm.server` ignores it.

Verify it before trusting it:

```
local/smoke-qwen38.sh
```

That checks health, generation, and tool calling, in that order. The tool-call check is the one worth caring about: `failover_keep_tools` hands Read, Edit, and Bash definitions to this tier, and a model that stops calling them looks healthy right up until failover gives it real work.

Long mode (`qwen38 start-long`, 262K context) is configured but unproven here. Upstream passed 261,888-token retrieval on an A100, never on this Mac, and the profile guard wants 25GB free disk before it will run.

#### The 27B tier must be GGUF, not int4/MLX

This tier used to be built `FROM qwen3.5:27b-int4` and was recorded here at **15 GB**. That number was real but it was a *floor*, not a steady state, and the difference took this host down.

Ollama serves int4/mlx tags on its MLX engine, and **that engine applies `num_ctx` from neither the Modelfile nor `OLLAMA_CONTEXT_LENGTH`** — it loads the model's native window and spawns its runner with no `--ctx-size` at all. So the tier ran at 262144 instead of the 32768 it was configured for, in two places, and nothing reported an error. Measured 2026-08-12 on 0.23.4, one server with one env:

| Tag | Engine | Native | `num_ctx` on tag | `OLLAMA_CONTEXT_LENGTH` | Loaded at |
|---|---|---|---|---|---|
| `phi4-mini:3.8b` | GGUF | 131072 | *unset* | 32768 | **32768** ✅ |
| `qwen3.5:27b-bare` (int4) | MLX | 262144 | 32768 | 32768 | **262144** ❌ |
| `qwen3.5:27b-bare` (GGUF) | llama.cpp | 262144 | 32768 | 32768 | **32768** ✅ |

`phi4-mini` carries no `num_ctx` of its own, so its clamp can only have come from the env — which is what proves the env reaches the GGUF path and is ignored on the MLX one.

**Re-run the `phi4-mini` row after every Ollama upgrade.** It is the cheapest possible proof that the clamp this whole section depends on still works, and an upgrade is exactly when it could silently stop. Done on 2026-08-16 going 0.23.4 → 0.32.13: `phi4-mini` still loaded at **32768** against its 131072 native window, so 0.32.13 still honours `OLLAMA_CONTEXT_LENGTH` on the llama.cpp path.

MLX also allocates KV *lazily*, so the tier loaded at the advertised 15 GB and grew toward **32 GB** as a session filled its 262K window. On a 36GB host that is the whole budget. The symptom is macOS's "Your system has run out of application memory", and it is deliberately hard to attribute: a 100%-GPU Ollama daemon shows up in neither the Force Quit list nor a RAM-percentage readout, so both indicators look calm while the machine dies.

The GGUF build costs **8 GB more at rest** (23 GB vs the old 15 GB floor) and that is the correct trade: 23 GB bounded beats 15 GB unbounded. The window is now enforced, so the number stops moving. Tell the formats apart from the manifest — GGUF is a single `model` layer, MLX is many small `tensor` layers:

```
curl -s -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
  https://registry.ollama.ai/v2/library/qwen3.8/manifests/27b | jq -r '.layers[].mediaType'
```

Check this **before** pulling; it costs one request and it is the only thing standing between you and a 262144-window load. `qwen3.8:27b` returns `license`, `model`, `params`, `projector` — one `model` layer, so GGUF. The sibling `qwen3.8:27b-mlx` is the trap.

The `projector` layer is new in 3.8: the tag is multimodal, which is why it is 17.7 GB against 3.5's 17.4 GB. Bare mode never sends it an image, so on this tier the projector is dead weight to budget for rather than a feature to plan around.

**Verify the window with `ollama ps`, never with `ollama show --parameters`.** `--parameters` reads the tag's stored value and answers "was `num_ctx` written", which stayed a reassuring 32768 for the entire time the model was actually running at 262144. Only `ps` reports what the running instance loaded. An earlier revision of this file had that backwards, which is why the regression went unnoticed.

Headroom exists because llm-jury reads `~/.backdoor/failover-state.json` and stands down while failover is active, so nothing else holds the GPU. Ollama caps residency by model count and never by bytes, and Metal allocations are wired and cannot be paged out, so over-committing panics this host rather than raising OOM. It did, twice, on 2026-07-31.

The profile sets `PROVIDER_REASONING_EFFORT=none` to suppress thinking traces for latency. Re-verified on qwen3.8 under Ollama 0.32.13 (2026-08-16): the model still emits a correct `get_weather` call with reasoning suppressed. Re-verify after an Ollama or model upgrade, because a tier that silently stops calling tools looks fine until you need it.

**Test the `/v1` path, which is what the profile uses.** Passing `reasoning_effort` as a top-level `/api/chat` argument did not suppress thinking here, so checking only that endpoint reports a failure that isn't real. On `/v1/chat/completions` the `reasoning` field came back empty and `tool_calls` was populated, which is the pass condition.

### Durable memory on local models

Local sessions read Mem0 recall from the offline mirror at `~/.mem0-local/cache.db` and get it prepended to the system prompt as plain text. No MCP server, no tool call, no network.

This exists because of a gap that was invisible from either side. Memory normally arrives through the `UserPromptSubmit` hook, which is why `bare.py` puts Mem0's MCP tools on the dropped side of the keep-list — the hook already injected the text before the request was built. But the `qwen` wrapper's lean and fast modes pass `--bare`, and `--bare` disables CLAUDE.md discovery and **every hook**. So the default local tier, the 27B, was the only brain in the stack with no durable memory, while `/model qwen`, failover, and `qwen full` all had it.

| Path | Before | Now |
|---|---|---|
| `/model qwen` (router) | hook injects | unchanged |
| Cloud→local failover | hook injects | unchanged |
| `qwen full` | hook injects | unchanged |
| `qwen`, `qwen lean` | **nothing** | proxy injects |
| `qwen fast` | **nothing** | proxy injects |

The proxy is the one place every local request passes through whichever door it came in by, so one code path covers all of them.

Reading the local mirror rather than the Mem0 API is deliberate: the cloud endpoint is unreachable during exactly the outage failover exists to cover, since the breaker opens on one condition — this host being offline.

```
PROVIDER_BASE_URL=http://localhost:11434/v1   # injection only fires for local providers
MEMORY_INJECT=false                           # turn it off
MEMORY_TOP_K=6
MEMORY_CHAR_BUDGET=1200
```

Cloud providers are excluded so a session whose hook already ran does not pay for the same text twice. Recall is read-only (`mode=ro`), fails open on a missing or locked cache, and times out in 1.5s so the Mem0 sync job's write lock cannot stall a turn. The budget is small on purpose: bare mode exists to hold the prompt near 945 tokens, and unbounded recall would rebuild the problem it solved.

Memories are labelled as background that may be stale rather than as instructions, because they are. The mirror still describes the heavy tier as `qwen3.5:27b-bare` at 15 GB, two facts that are both now wrong, and a local model asked about the tier will say so rather than assert the stale version.

### Building a bare tag

Build from the **registry** tag, never from a local one. `modelfiles/build.sh` appends a ~43K-token system prompt to every `*.Modelfile` it builds and defaults to building all of them, so every local `qwen3.5:*` tag carries 186,647 bytes of baked prompt. `FROM` inherits it, which makes a "bare" model that is nothing of the sort. The failure is not subtle but it is easy to miss: the first attempt here answered from the baked prompt's tool vocabulary and invented a `weather_fetch` tool the request never defined.

Bare Modelfiles therefore live in `modelfiles/bare/`, which a bare `./build.sh` cannot glob. Check any new tag with `ollama show --system <tag>`, which must return nothing.

#### The base tags were poisoned, which made that check lie

`build.sh` derives its tag from the filename with the first `-` turned into `:`, so `qwen3.5-9b.Modelfile` built **`qwen3.5:9b`** — the exact name `ollama pull` uses for the pristine base. It overwrote the registry pull with a prompt-baked copy, and from then on every `FROM qwen3.5:9b` in this directory inherited 43K tokens.

That is the same inheritance bug described above, but far harder to see: the Modelfile you read has no `SYSTEM` line anywhere in it, and neither does the one it inherits from. Only the tag does.

Found 2026-08-16 with `ollama show --system qwen3.5:9b` returning 186,647 bytes where it should return 0. `qwen3.5:4b` was poisoned the same way. Both are fixed, and the repair is cheap because only the manifest differs:

```
ollama pull qwen3.5:9b     # 4 seconds — the model blob was already on disk
```

`build.sh` now refuses any tag with no suffix after the colon, so it cannot happen again. If you want a persona build of a base model, give it a variant name (`qwen3.5:9b-fable`), which is the convention `llama3.1:8b-fable` and `gemma3:12b-fable` already follow.

**`SYSTEM ""` does not undo it.** Rebuilding `FROM` a poisoned base with an empty `SYSTEM` still reports 186,647 bytes. The base itself has to be re-pulled.

#### `qwen3.5:9b-64k` is built bare

Listed in `build.sh`'s `BARE_TAGS`. The baked prompt only applies when a request sends no system message, and both consumers of this tag (`/model qwen-9b`, the `fusion-qwen` subagent) always send one, so it bought them nothing. The one thing it could have served, bare `ollama run`, was unusable on a 9B:

| | prompt tokens | wall clock |
|---|---|---|
| With baked prompt | 43,092 | 8+ min, then `500` |
| Built bare | 12 | 2.4 s |

The 4B tiers keep theirs, since bare usage there completes.

### When `ollama pull` will not finish

`scripts/fetch-ollama-model.sh <model> <tag> [jobs]` pulls a library model by fetching its blobs directly, verifying each SHA-256, and writing the manifest last so a half-downloaded model can never appear in `ollama list`.

Reach for it when a pull stalls. `qwen3.5:27b-int4` is packaged as **1,193 layers**, 1,184 of them per-tensor, and `ollama pull` never finished it here: it transferred ~9MB, discarded the chunk, and restarted, twice over 15 minutes each. Neither disk (152GB free) nor bandwidth was the cause, since a ranged `curl` against the same blob sustained 20MB/s throughout. The problem is many small sequential transfers on a connection that drops, with `OLLAMA_MAX_TRANSFER_STREAMS=1` forbidding any overlap, so one stall halts everything.

Concurrency is the fix, because the cost is per-request latency rather than bandwidth. The script finished the same 16.1GB in about two minutes with 12 workers. It resumes: already-present blobs are skipped, so rerun it after an interruption.

#### The opposite packaging breaks it too

`qwen3.8:27b` is **four** blobs, one of them a single **16.81 GB** model layer. Per-blob concurrency does nothing there — one blob is one worker — and two assumptions in the original script were wrong for that shape:

- `--max-time 240` cannot transfer 16.81 GB at any speed this network reaches, so every attempt timed out mid-download and the old `rm -f "$part"` threw the bytes away before retrying. An infinite loop that looks like a slow network.
- No `-C -`, so nothing resumed.

Both are fixed. Large blobs now download as parallel byte ranges (`fetch_big`), each chunk separately resumable, with the reassembled blob SHA-256 checked before install — a misordered or short concat cannot pass. Transfers are now aborted on **throughput** (`--speed-limit`/`--speed-time`) rather than a wall-clock cap, which is size-independent.

Measured against the registry on 2026-08-16: one connection 435 KB/s, four ranged connections ~1.0 MB/s aggregate. The per-connection throttle is real, so ranges help — but there is a ceiling above it, and on a bad night the registry alone can put a 17 GB model into the multi-hour range no matter how you fetch it.

#### Two failure modes that look like something else

**A pull that transfers zero bytes and exits 0 is a too-old client.** `ollama pull qwen3.8:27b` on 0.23.4 prints only `Please download the latest version at: https://ollama.com/download`. It does not say "unsupported", it does not fail, and `echo $?` is 0. `qwen3.6:27b` pulls fine on the same daemon, so the version floor is per-model — 3.8 needs Ollama ≥ 0.32.

**A partial of exactly the right size can still be corrupt.** Ollama's abandoned `-partial` file for the projector blob was byte-for-byte the manifest's size, 931,146,016, and its SHA-256 did not match. Size is not verification; promoting that blob on size alone would have installed a broken model. Always check the digest before salvaging a partial.

**The breaker opens on exactly one condition: this machine is offline.** That is deliberately narrow, because failing over is not free — it loads a local model into Ollama, and on a machine that also runs a local council (see [llm-jury](https://github.com/Screddyice/llm-jury)) two GPU consumers at once will fight for memory.

So the trigger set is tight:

| Upstream behavior | Failover? | Why |
|---|---|---|
| Any HTTP response (`429`, `529`, `500`…) | **No** | A status code proves the request reached Anthropic and was answered. A usage limit is not a reachability problem, and hiding it behind a local model both masks a real signal and takes the GPU. The error is relayed so your client's own retry/backoff runs. |
| Transport error, host still online | **No** | Anthropic specifically is unreachable. Relayed verbatim so a provider outage stays visible. |
| Transport error, host offline | **Yes** | Nothing else is reachable either — local is the only way the session survives. |
| `401` / `403` | **No** | The network is fine and a credential is broken. Masking that would hide a revoked key indefinitely. |

Reaching the failure threshold is necessary but not sufficient: a TCP connectivity probe to a public address gets the final say, and it is re-taken each time (never cached), costing one probe per run of failures rather than one per request.

### How long failover takes

The breaker probes connectivity on the first transport failure. If the host is offline, the first
request moves to local failover after one bounded upstream retry. The local model's cold start is
then the main delay.

| Stage | Cost |
|---|---|
| One router-level retry, bounded by the upstream timeout | depends on the failed operation |
| TCP probe confirming the host is offline | ~0s offline (fails instantly), up to 4s otherwise |
| `qwen3.5:27b-bare` cold start | ~10s |

The connectivity probe, rather than a retry count, prevents a provider-only blip from claiming the
GPU. The default threshold is one so Claude does not have to display an error before the router
checks whether local failover is permitted.

**Local retries are serialized per profile.** When Claude retries while Ollama is still loading or
prefilling, Backdoor queues the duplicate request instead of opening another connection and
flooding Ollama. Loopback providers get a 120-second connect allowance and a 600-second pool wait;
cloud providers keep the shorter transport limits.

**If nothing fails over at all, the session is almost certainly not routed.** Check `ANTHROPIC_BASE_URL` on the running process rather than the shell:

```
ps -E -o command= -p $(pgrep -x claude | head -1) | tr ' ' '\n' | grep ANTHROPIC_BASE_URL
```

Claude Code reads that variable once at startup, so a session that began unrouted can never gain failover — restarting the router or the shell will not reach it, and only relaunching does.

**Coordination with other model consumers.** Every breaker transition is published atomically to `~/.backdoor/failover-state.json`:

```json
{ "failover_active": true, "reason": "ConnectError", "updated_at": 1754130000.0, "pid": 4242 }
```

Claude and Codex also write a process-scoped lease under `~/.backdoor/compute-leases/` before `qwen3.8:27b-obliterated` starts inference. This covers explicit Qwen sessions as well as failover and closes the load-time gap before Ollama lists the model in `/api/ps`. LLM-Jury checks the breaker, active leases, and Ollama residency before it constructs any backend. If the 27B model owns compute, LLM-Jury disables the local council and all frontier providers, including OpenRouter. Expired leases and leases from dead router processes are ignored.

Writing is best-effort. A router that cannot publish state still routes, and LLM-Jury treats missing or unreadable state as inactive. Ollama residency remains the final backstop after a lease expires.

| Setting | Default | Effect |
|---|---|---|
| `failover_to_local` | `true` | Master switch for hybrid-mode failover |
| `failover_bare` | `true` | Strip the harness off a failed-over request. Turn off only together with reverting the tier to a 4B |
| `failover_keep_tools` | `local` | What survives the tool list. `local` keeps everything not prefixed `mcp__`; add comma-separated substrings to keep specific MCP tools; empty keeps none |
| `failover_tool_result_chars` | `2000` | Per-tool-result character budget in the stripped transcript |
| `failover_threshold` | `1` | Transport failures before the connectivity probe runs. The probe, not this count, stops a transient blip from opening the breaker |
| `failover_window_seconds` | `120` | Failures outside this window start a fresh run |
| `failover_probe_seconds` | `60` | How often an open breaker retries upstream (half-open) |
| `BACKDOOR_FAILOVER_STATUSES` | *(empty)* | Comma-separated HTTP statuses to restore as triggers, e.g. `429,529` |
| `BACKDOOR_FAILOVER_STATE` | `~/.backdoor/failover-state.json` | Where breaker state is published |
| `BACKDOOR_COMPUTE_LEASE_DIR` | `~/.backdoor/compute-leases` | Where Claude and Codex publish exclusive 27B ownership leases |

---

## Design specs

Larger changes get written down before they get built. Specs live in [`docs/specs/`](docs/specs/).

| Spec | Status | What it covers |
| --- | --- | --- |
| [Hermes MCP Bridge](docs/specs/hermes-mcp-bridge.md) | Implemented | An HTTP MCP surface for [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateways, so an MCP client can list and control them, converse with an agent, read its history, and answer its run approvals |

The bridge ships as `src/hermes_mcp/`, a sibling concern that never touches
`src/proxy/`. It is off by default everywhere: the service is not installed by
this repo, and `qwen` attaches it only when `QWEN_HERMES=1`. Configure it with
a registry file (`deploy/registry.example.toml`) and one authentication mode.
Static bearer deployments set `HERMES_MCP_KEY`; browser connector deployments
set the OAuth issuer, password, and redirect-host allowlist described below.
Deployment identifiers live outside this repo, the way `profiles/*.env` already does.

Approved specs get an implementation plan before any code, in
[`docs/superpowers/plans/`](docs/superpowers/plans/). A plan is task-by-task and test-first, with
the actual test and implementation code in each step rather than a description of it, so it can be
executed by someone — or something — with no prior context on this repo.

| Plan | Tasks | For |
| --- | --- | --- |
| [Hermes MCP Bridge](docs/superpowers/plans/2026-08-15-hermes-mcp-bridge.md) | 9 | [the spec above](docs/specs/hermes-mcp-bridge.md) |

The Hermes bridge is worth a note here because it changes what this repo is. Backdoor has been one
thing so far: a proxy that makes Claude Code talk to any model. The bridge adds a second, separate
concern that does not touch `src/proxy/` at all, and brings two conventions with it.

**Deployment identifiers stay out of the repo.** Hostnames, agent profile names, port assignments
and keys are supplied at deploy time, the same way `profiles/*.env` is already gitignored. A spec
in `docs/specs/` describes a design, never an environment.

**It ships with `bd` and `qwen` wiring.** The bridge gets a `bd hermes` subcommand for local
operator use, and rides the existing opt-in MCP mechanism in the `qwen` wrapper (`QWEN_HERMES=1`)
rather than being attached by default. MCP stays off by default in every tier for the reason
documented above: the global schema set costs roughly 142K tokens, which is most of what bare mode
exists to remove.

The MCP endpoint is served at path **`/mcp`** on whatever host and port the bridge binds. A
reverse proxy must also route the OAuth discovery, registration, authorization, token, and login
paths when browser-based clients such as Claude use OAuth mode.

**Authentication modes.** A deployment selects one mode. `HERMES_MCP_KEY` keeps the original
static bearer-token path for clients that can set an `Authorization` header. Setting
`HERMES_MCP_OAUTH_ISSUER` selects the browser connector path instead. OAuth mode enables dynamic
client registration, authorization-code flow with PKCE, one-owner password consent, one-hour
access tokens, and rotating 30-day refresh tokens. Registered clients and tokens survive service
restarts in a mode-600 state file. The login password and deployment identifiers remain outside
the repository. Successful password submissions use an HTTP 303 redirect so embedded browsers
follow the Claude callback with GET instead of replaying the login form POST.

**Environment.** Set at deploy time, never committed:

| Setting | Required | Effect |
| --- | --- | --- |
| `HERMES_MCP_KEY` | Static only | Bearer key callers must present. Boot refuses on a missing, short (under 16 characters), or placeholder-looking value |
| `HERMES_MCP_OAUTH_ISSUER` | OAuth only | Public HTTPS origin for the bridge, such as `https://hermes.example.com`. When set, OAuth replaces static bearer authentication and `HERMES_MCP_KEY` is not required |
| `HERMES_MCP_OAUTH_PASSWORD` | OAuth only | Password Shawn enters on the connector authorization page. Boot refuses missing, short, or placeholder-shaped values |
| `HERMES_MCP_OAUTH_REDIRECT_HOSTS` | OAuth only | Comma-separated redirect-host allowlist for dynamic registration, such as `claude.ai,claude.com`. Registrations for other hosts are refused |
| `HERMES_MCP_OAUTH_STATE_PATH` | No | Mode-600 JSON file holding OAuth client registrations and tokens. Defaults to `~/.config/hermes-mcp/oauth-state.json` |
| `HERMES_MCP_REGISTRY` | No | Path to the profile registry TOML. Defaults to `~/.config/hermes-mcp/registry.toml` |
| `HERMES_MCP_HOST` | No | Address the bridge binds. Defaults to `127.0.0.1`. A non-loopback address turns **off** the SDK's automatic loopback-only DNS-rebinding protection, so `HERMES_MCP_ALLOWED_HOSTS` is **required** with one: the bridge refuses to start on a non-loopback bind with an empty allowlist rather than serving unprotected. Loopback (`127.0.0.1`, `localhost`, `::1`) needs no allowlist |
| `HERMES_MCP_PORT` | No | Port the bridge binds. Defaults to `8000`. A non-integer or out-of-range value is refused at boot rather than surfacing later as a bind failure |
| `HERMES_MCP_ALLOWED_HOSTS` | No | Comma-separated, whitespace-tolerant extra `Host` values for the streamable-HTTP transport's DNS-rebinding allowlist, e.g. `bridge.example.com:443`. Unset or empty leaves today's behavior unchanged — the SDK's own loopback-only default applies (`127.0.0.1:*`, `localhost:*`, `[::1]:*`). Set it and those loopback defaults stay, with the configured hosts added on top; DNS-rebinding protection stays on, the allowlist only widens. Needed when the bridge runs behind a tunnel that forwards a public hostname in the `Host` header — without it such a request is rejected with **421 before auth even runs**. Required, not optional, whenever `HERMES_MCP_HOST` is non-loopback |

For OAuth mode, proxy these paths to the same bridge process: `/mcp`, `/login`, `/authorize`,
`/token`, `/register`, and `/.well-known/oauth-*`. Claude's custom connector needs only the public
`https://.../mcp` URL; leave its advanced client credential fields empty so Claude uses dynamic
registration.

---

<div align="center">

**Star this if you think the best coding agent should work with any model.**

</div>
