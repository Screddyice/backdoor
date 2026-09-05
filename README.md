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

**A handshake that times out gets a second, patient attempt** (added 2026-09-03). Reading every
non-verifying outcome as the middlebox lie was the mirror image of the 2026-08-17 hole, and it cost
a full evening: on a tethered link measuring 744 ms average RTT with 3.1 s spikes, an honest peer
cannot finish inside the 2 s budget, so the probe reported "offline" three times against a working
internet and the breaker claimed the GPU each time. Every one of those opens logged `The handshake
operation timed out`; not one logged a certificate error, which is what tells the two apart.

So a timeout is now treated as *inconclusive* rather than as an answer, and the probe re-dials once
with `CONNECTIVITY_SLOW_FACTOR` × the budget (8 s by default). A genuinely silent acceptor never
completes a handshake at any budget, so it still reads as offline; a real peer on a stalling link
finally gets enough room to answer. A certificate error is a definite answer and is never retried.
The extra wait is paid only on the timeout path, and only on a request that is already failing.

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

### The probe also asks whether DNS answers

`internet_usable` is what the offline-gated breaker calls, and it asks two questions in order: does
a link probe verify a peer, and does `name_resolution_works` get an address back for
`one.one.one.one` or `dns.google`. Either half missing means offline. Resolution goes second, so the
common outage still costs one TCP probe and no lookup, and a dead link never gets logged as a DNS
fault.

The literal-IP probes left DNS unmeasured, and that was its own silent hole. On 2026-09-04 between
23:36:32 and 23:39:41 every upstream request failed with `[Errno 8] nodename nor servname provided`
while the link stayed up. `internet_reachable` verified a peer on the first probe each time, so the
Anthropic breaker read the host as online, logged `Anthropic failing (ConnectError) but this host is
online`, and handed roughly 200 requests a `502` apiece across three and a half minutes. The Codex
breaker never consults connectivity, opened at 23:36:39, and served the same outage from the local
model. One host, one outage, opposite outcomes.

`getaddrinfo` takes no timeout and `socket.setdefaulttimeout` does not reach it, so the lookup runs
on a daemon thread the router stops waiting for after `RESOLUTION_TIMEOUT` (8 s, the same patient
budget the ambiguous handshake gets). A resolver that accepts queries and never replies reads as
broken without stalling the turn, and the abandoned thread cannot block a restart.

| Setting | Default | Effect |
| --- | --- | --- |
| `RESOLUTION_PROBE_NAMES` | probe cert names | Names tried; any one answering proves the resolver works |
| `RESOLUTION_TIMEOUT` | `CONNECTIVITY_TIMEOUT × CONNECTIVITY_SLOW_FACTOR` | How long a lookup gets before DNS reads as broken |

### Last-known-good DNS

`src/proxy/resolver.py` wraps `socket.getaddrinfo` for the router process. A
successful lookup is remembered for six hours; a lookup that fails with a resolver
error is answered from that memory rather than raised. `install()` runs first thing
in the app lifespan, before any client exists.

This machine has one resolver, the LAN gateway DHCP hands out, and 722 of the 731
upstream transport failures in the router log are `[Errno 8] nodename nor servname
provided`. The link was up for all of them. An address the router already knows is
enough to get those requests through.

Serving an old address costs nothing in safety: the connection still verifies TLS
against the hostname that was asked for, so an address that has moved on to someone
else fails the handshake instead of being trusted. If it is simply dead, the request
fails as it would have anyway and the breaker takes over.

**This layer exists so the breaker does not have to fire.** Opening it loads a 17 GB
tier into the Ollama server the llm-jury council needs, which `failover.py` allows
only when local is the one way a session survives. A cached address that connects
means it was not, so the session stays on the cloud model and the GPU stays free.

The failover DNS probe is deliberately exempt. `name_resolution_works` calls
`resolver.system_getaddrinfo()` to reach the stdlib directly, because a probe asking
whether DNS answers cannot be answered from a cache of the times it did.

| Setting | Default | Effect |
| --- | --- | --- |
| `BACKDOOR_DNS_CACHE` | unset | Set to `0` to disable and leave `socket.getaddrinfo` alone |
| `CACHE_TTL` | 6 h | How long a remembered address stays usable |
| `CACHE_MAX` | 256 | Entries kept before the oldest is dropped |

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
at once with Esc and resend. Claude and Codex agents must not restart this service: the router is
their repair path, so a failed restart can strand both clients. Machine-level pre-tool hooks block
live launchd, deploy-checkout, dependency, and process mutations while leaving read-only health
checks available.

**The router is serving code you did not just edit**
The `:8083` router runs from a *separate* deploy checkout (`backdoor-service`), in detached HEAD,
not from your dev clone. Editing the dev clone changes nothing until a human performs a live
deployment from an independent Terminal session. Agents may inspect the checkout and prepare a
tested commit, but the live checkout and launchd job are user-operated control-plane state.

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

After an eligible failure persists for 20 seconds, the Codex breaker routes the turn to `qwen3.8:27b-obliterated` through Ollama. Transport failures and `429,500,502,503,504,529` responses count. HTTP `400`, `401`, and `403` never count, so a malformed request or broken login remains visible instead of being hidden by Qwen.

Cloudflare can also lose a valid provider route and return a bare `404` with a
`cf-ray` header. Backdoor counts that unstructured response as outage evidence
for both Codex and Claude, even when other internet hosts remain reachable. It
still relays structured JSON `404` errors, such as an invalid model, so failover
does not conceal a real client or API error. Claude transport failures keep the
existing whole-machine connectivity gate.

The visible Codex thread does not change. Qwen receives a fresh internal request containing:

- the latest user instruction and the active local tool loop, including a paired tool continuation that does not repeat the user message;
- a bounded recall from the local claude-mem replica when `QWEN_MEMORY` is enabled;
- local Code Mode tools converted from Codex's Responses Lite namespace format.

It does not receive the old cloud transcript, cloud reasoning context, prompt-cache identifiers, OAuth headers, remote MCP schemas, hosted web-search tools, or image and file attachments. Backdoor reads the synced local replica at `~/.claude-mem/claude-mem.db`, so recall works without a network call or memory tunnel. Durable fetched-source storage remains off unless an operator configures reviewed public URL prefixes; authenticated, browser-session, malformed, and unpaired tool results remain ephemeral. Set `QWEN_MEMORY=0` to suppress memory reads and writes on the local path.

Backdoor sets `reasoning.effort=none` on the local Responses request so Ollama answers with a plain assistant message. Ollama's own reasoning items carry `encrypted_content` signed locally, which ChatGPT cannot verify once the breaker closes; a thread carrying them cannot go back to cloud inference. Keeping them out of Codex history is what lets the same task return to cloud.

The breaker permits one ChatGPT probe every 60 seconds while open. A probe closes it the moment ChatGPT returns response headers, the same point the Anthropic path uses: a status line proves this host can still reach the upstream, and reachability is the only question the breaker asks. Later turns return to cloud immediately. Qwen is released separately, once a relayed stream finishes, because a probe whose body dies mid-flight is about to be retried and unloading the tier that will serve that retry only buys a reload.

Crediting the probe at the end of the stream instead looks equivalent and is not, because the failure is silent. On 2026-08-30 a blip opened the Codex breaker at 23:09:48. Probes at 23:13:46 and 23:15:23 both reached ChatGPT and logged `path=cloud status=200`. The breaker stayed open for 19 minutes anyway, answering two Codex turns from the local 27B in 309s and 385s while the cloud was fine. Both clients hung up before their bodies finished, and closing a generator raises `GeneratorExit` at the `yield`, which runs neither the transport-error branch nor the success branch. A probe that plainly worked was discarded twice. A client that hangs up before the first chunk never starts the generator at all, so no handling inside the body can see that probe succeed.

Before Codex receives a local response, Backdoor removes Qwen reasoning items from streaming SSE and non-streaming JSON. It reindexes retained SSE output and removes every local `encrypted_content` field. Ollama places plaintext reasoning in that field, while ChatGPT expects an opaque value that it issued and can verify. Letting a local reasoning item enter the thread makes the first recovered cloud turn fail with `invalid_encrypted_content`. Tool calls and visible assistant output remain in the thread; healthy cloud responses still pass through byte-for-byte.

Backdoor caps each local SSE frame and non-streaming JSON body at 8 MiB. It requests uncompressed local responses and rejects any encoded response before reading its body, which prevents decompression from bypassing those limits. Invalid or oversized JSON returns `502 Local Qwen returned an invalid response`. An invalid or oversized SSE frame closes the local stream and releases its runtime slot.

### The local 32K allocation

Backdoor enforces this allocation on the fresh request that it sends to Qwen:

| Component | Limit |
| --- | ---: |
| Local guidance | 1,000 tokens |
| claude-mem recall | 2,000 tokens |
| Local tool schemas | 4,000 tokens |
| Current task and active tool results | 21,000 tokens |
| Reply reserve | 4,000 tokens |

Backdoor removes extra recall, optional tools, old tool output, attachments, then the oldest assistant updates and completed call/output pairs. It keeps the latest textual instruction and the newest progress from the active turn. If trimming removes every pair from a tool-only continuation, Backdoor inserts a small continuation instruction instead of sending Qwen an empty task. Codex receives HTTP 413 only when the latest textual instruction or a required tool cannot fit.

Set `CODEX_LOCAL_TOOLS=` to remove all local tools. The default `local` keeps non-MCP Code Mode tools. Remote `mcp__*` namespaces stay out of the outage request because they cannot work without the network.

Set `CODEX_FAILOVER_TO_LOCAL=false` to disable Qwen fallback without removing the custom provider. Cloud inference still crosses the Responses relay and returns its real errors. Backdoor logs correlation IDs, route choice, status class, timing, cloud request byte counts, local token counts, recall counts, and tool counts. It never logs prompts, recalled text, OAuth values, tool arguments, or model output.

---

## Offline failover (hybrid mode)

### Keep Codex Desktop active while offline

Codex Desktop can confuse a failed ChatGPT token lookup with an explicit logout. When that happens, the app switches to its login screen and pauses the local task stream before Backdoor receives an inference request. The offline-auth compatibility patch keeps a saved identity active when the token lookup is temporarily unavailable. A real logout still clears the underlying Codex identity.

Install it against the signed application bundle:

```bash
uv run python local/patch-codex-desktop-offline.py
```

The installer validates the `com.openai.codex` bundle, requires one known renderer expression, and creates a verified backup for the installed version and build. It patches a staged copy without moving ASAR offsets, ad-hoc signs and verifies that copy, then swaps the complete bundle into place. A process lock serializes installs. If Codex updates during activation, the installer verifies the competing bundle before choosing whether to restore or preserve it for recovery. Restart Codex Desktop after installation. OpenAI application updates replace the patch, so rerun the installer after each update until the upstream app distinguishes offline token errors from logout.

| Option | Default | Use |
| --- | --- | --- |
| `--app` | `/Applications/ChatGPT.app` | Patch or restore a different Codex Desktop bundle. |
| `--backup-root` | `~/Library/Application Support/Backdoor/Codex Desktop Backups` | Store full-bundle and ASAR backups elsewhere. |
| `--restore` | Off | Restore the verified original bundle for the installed version and build. |

Changing the bundle replaces OpenAI's publisher signature with an ad-hoc signature. Structural signature verification still passes, but macOS no longer sees OpenAI's original designated requirement or TeamIdentifier. After restarting, confirm that Codex Desktop launches, stays signed in, and can read its existing tasks before testing an outage. If launch, login, or Keychain access fails, restore the signed bundle:

```bash
uv run python local/patch-codex-desktop-offline.py --restore
```

The installer retains one full rollback bundle per Codex version and build. Remove older backups by hand only after the current build passes the restart and offline canaries.

The patch keeps the Desktop task stream alive. Backdoor still decides whether an inference request uses ChatGPT or local Qwen. Online Qwen requests can opt into the specific MCP tools they need; internet-dependent MCP servers remain unavailable during a genuine network outage.

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

Recording a failure never opens the breaker on its own. `internet_usable()` still decides that, and these routes never call `record_success`: closing the breaker obliges the caller to unload the tiers it claimed, and only the `/v1/messages` path knows how.

### Deploying: scripts/deploy-router.sh

`DRY_RUN=1` prints the plan and skips the quiet-window wait, because a dry run restarts nothing and
therefore has no in-flight requests to protect. That wait is also the one step a dry run could never
finish: a live Claude session writes to the router log every few seconds, so the window never opens,
and asking to see the plan would end in `ABORT: still busy after 180s` with the plan never printed.

A real run still waits. When it cannot get a quiet window — another session is mid-turn, and on this
machine one usually is — `FORCE=1` proceeds and drops whatever is in flight. There is no
zero-downtime path here, so that is the honest trade rather than a workaround.


Deploying the router is a fast-forward and a restart, and on 2026-09-03 doing it as a list of shell lines went wrong in the dullest way available: the two git steps failed, the restart at the bottom ran anyway, and the router came back on unchanged code having dropped every in-flight request. Every live session reported an API error. Nothing deployed, and something still broke.

`scripts/deploy-router.sh` exists so that cannot happen. It cannot half-run:

- every step is gated on the previous one, including the restart command being present — checked in preflight, because discovering it late would leave the checkout advanced and the old code serving
- a checkout already at the target exits `0` **without restarting**; a restart that changes no code is pure cost
- it waits for the log to go quiet first, since a restart kills in-flight requests and there is no zero-downtime path here (socket activation on 8083/8084 is forbidden on this machine after the 2026-08-31 experiments were reverted)
- it verifies the **new** code is running by finding the startup marker in the log written *after* the restart, not merely that something restarted
- it rolls back and restarts again automatically when that verification fails

The restart itself is not in the file. It arrives in `RESTART_CMD`, supplied by whoever runs the deploy, because operating this machine's live control plane is a human's job and the repo should not encode one host's launch agent.

```bash
RESTART_CMD='<restart the router>' scripts/deploy-router.sh <service-checkout-dir> [ref]
```

`ref` defaults to **`origin/main`**. It pointed at the deployed feature branch until 2026-09-03, when that branch merged into `main` — and a default aimed at a merged branch is worse than a wrong one, because the branch never advances again and the script correctly reports "already at the target" forever while the router falls further behind.

`DRY_RUN=1` prints the plan and changes nothing. `FORCE=1` skips the quiet wait and accepts that in-flight requests will fail. `QUIET_SECONDS` / `QUIET_TIMEOUT` tune the wait.

The service checkout on this machine is a **detached worktree** sharing one repository with about twenty others, so the script fast-forwards the detached HEAD and never runs `git checkout <branch>` — that collides with whichever worktree holds the branch.

### The ladder always answers

`pick_failover_profile` ends in `return FAILOVER_LADDER[-1][1]`, so an oversized session gets the widest tier rather than nothing. That is the guarantee worth holding: **an outage never leaves a session with no local tier at all.**

It used to be asserted as `bounds[-1] == float("inf")` — the shape of the data rather than the behaviour, and redundant with that trailing return. The test now asserts the behaviour, which both passes today and fails loudly for anyone who changes the selector to return `None` for a session no tier can serve. That is a legitimate design (it pairs with shrinking the prompt before the selector runs, so an impossible request gets an honest answer instead of a model call that cannot complete) — but it is a change in contract, and a failing test is where that should surface.

### Opening needs a sustained outage, not a fast one

Every Claude failover in the log used to read `after 1 consecutive failures`, and raising that number would not have helped. Requests are concurrent, so a consecutive-failure count is satisfied in a single instant — measured 2026-09-03 at `14:46:18.113`, four transport failures landed in the same millisecond, all `[Errno 8] nodename nor servname provided` from one DNS hiccup. Any threshold trips on that burst. Counting never told a two-second blip apart from an outage; elapsed time does.

Both clients require the upstream to fail for 20 seconds before their breakers
may open. The gate runs before the connectivity probe, so a burst does not fire
one TLS handshake per failure.

The shared 20-second gate is the operator policy for Codex and Claude. During
that window, each client retains its normal retry or error behavior.
Authentication and request errors still bypass failover on both paths.

During the gate the real errors are relayed, and Claude Code retries through
them. That avoids moving a live Claude session onto a local
model for a short link stall.

Worth knowing when reading the log: **a short outage shows as a long open.** Half-open only retries once per `failover_probe_seconds`, so a five-second blip can appear as a 60–90 second open with nothing wrong.

### One notification per outage, not per transition

Sixteen desktop popups on the evening of 2026-09-02. Every transition is still logged in full; the notification is for the human and is rationed to one per breaker per `failover_notify_cooldown_seconds` (900 s). A close announces only if its open did, so a suppressed outage never leaves an orphan "back to cloud" for something you were never told about.

This is load-bearing rather than politeness: the recovery ticker below makes transitions *more* frequent, so without rationing it would have made the noise worse.

**A cooldown alone still let one outage speak twice.** It rations a single breaker over time, and a dropped link is not a single breaker — it takes every upstream at once. The outages at 22:13 and 23:43 on 2026-09-03 each produced four popups for one Wi-Fi blip: Anthropic open, Codex open, Anthropic closed, Codex closed. So a breaker also stays quiet when a peer on the same router is already open. Whichever gets there first says "routing to local model"; the rest log the transition and say nothing, and their closes stay silent under the same orphan rule. Replaying that night's real transitions turns 10 popups into 4.

A breaker that fails while every peer is healthy still speaks — that is a report about one upstream, not a duplicate. Codex timing out on a working link at 23:57 that same night was the only news there was, and it still announces.

Worth knowing when a burst of these shows up at once: **macOS holds notifications through a Focus or a scheduled summary and releases them in a batch**, stamped with the release time rather than the outage. A stack of popups reading "1m ago" can be a backlog going back days — 74 transitions accumulated between 2026-08-26 and 2026-09-04. Check `~/.backdoor/failover-state.json` for the truth: `failover_active` and an `updated_at` that only moves on a real transition.

| Setting | Default | Effect |
| --- | --- | --- |
| `failover_min_outage_seconds` | `20.0` | How long the Claude upstream must stay broken before failover may open |
| `failover_notify_cooldown_seconds` | `900.0` | Minimum gap between failover notifications, per breaker |
| `codex_failover_min_outage_seconds` | `20.0` | How long the Codex upstream must stay broken before failover may open |
| `codex_failover_notify_cooldown_seconds` | `900.0` | The same rationing for the Codex breaker |

### Recovery does not wait for traffic

An open breaker used to have exactly one way back: `allow_upstream` hands a real `/v1/messages` passthrough a half-open slot once per `failover_probe_seconds`, and only that request's success closes it. Recovery therefore depended on the traffic an outage takes away — a session being served by a local 4B generates far less of it, and a session you walked away from generates none.

Measured on 2026-09-02, the breaker was open from 22:28:14 to 23:45:51 — 77 minutes 37 seconds, most of them on a network that had already recovered — and it closed at the exact second unrelated test traffic reached it. For that whole window every routed session was quietly answered by qwen instead of the cloud model, which reads as "the API errors never stopped" rather than as a stuck breaker.

A background ticker now re-runs the connectivity probe every `failover_probe_seconds` while the breaker is open, and closes it the moment this host is online again — releasing the local tiers exactly as an upstream success would. It is the negation of the condition that opened the breaker: opening means "this host is offline", so recovery is "that stopped being true". If Anthropic itself is still down, the next real request fails, the probe finds the host online, and the error is relayed — the documented behaviour for an upstream outage on a working link. The ticker does nothing at all while the breaker is closed, and starts only when `failover_to_local` is on.

The ticker watches **both** breakers this router owns — the Claude one and the Codex one — each released through its own tier path.

**Each breaker is answered by the probe that matches its premise.** The Claude breaker opens because this host had no route out, so connectivity is the exact negation of what it concluded. The Codex breaker never consults connectivity at all — `codex_failover_require_offline` is `false`, so it opens on consecutive service failures and can be open while the network is perfect. Answering it with a connectivity probe would be answering a question it never asked. It gets `service_reachable()` instead: the same verified handshake, aimed at its own upstream host.

Unlike `internet_reachable`, that one resolves DNS on purpose. Literal IPs exist so a broken resolver cannot fold itself into "has this machine got a route"; "is *that service* reachable from here" is a different question, and a name that will not resolve is a real way for a service to be unreachable.

**Reachability disproves "I could not reach it" and never "it answered 429".** The front door of a rate-limited service is perfectly reachable, and this router cannot check the difference on its own: it relays the caller's credentials and holds none, so it cannot make an authenticated Codex request outside a real one. So `record_failure` takes `transport_error`, passed `False` at the two `f"HTTP {status}"` call sites, and the breaker remembers which kind opened it. A transport-error open can be reconsidered by the probe; a status open keeps the half-open path, which is the only route that actually settles a quota and the only one carrying a credential.

The practical difference: an idle Codex outage that was a transport failure now ends on its own, instead of holding a qwen tier resident and llm-jury disabled until traffic happens to return. A usage limit still waits for a real request, which is correct.

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


### A local tier that stops answering

`API Error: 500 Internal server error` is what Claude Code shows you when a server breaks, and until now Backdoor could write one itself.

Both local-provider call sites caught `ProviderError`, which covers a status Ollama returned and nothing else. A transport failure matched neither clause. In practice that meant `httpx.ReadTimeout` against a tier still prefilling past the 600s read budget: the exception escaped to uvicorn, uvicorn answered `500`, and the router log named neither the tier nor the timeout. It fired 62 times between 2026-08-26 16:19 and 2026-09-02 23:56, the last stretch of them every ten minutes on a scheduled caller.

Both paths now answer for themselves:

| | |
|---|---|
| Completion | `504` on a timeout and `502` on anything else, with the tier name in the body, so your client keeps its own retry and backoff |
| Stream | An SSE `error` event, because the headers left with the first heartbeat and no status remains to send |
| Either | One `Provider transport failure on <tier>` line carrying the exception name |

The cloud side had the quieter half of the same problem. A relayed upstream error carried no status into the log, so a `500` from Anthropic and a turn that worked both printed `→ passthrough [claude-opus-5] /v1/messages` and nothing more. One session hit that on 2026-09-03 at 21:24, and the log could not tell you which side wrote the error. Backdoor now logs `upstream POST /v1/messages → 500 (relayed verbatim)` at WARNING for every relayed status at or above 400. Healthy turns still cost one line each.


### The breaker's verdict does not run on the event loop

`record_failure` calls `internet_reachable`, a blocking socket probe against a public address. It
is the thing that decides whether a run of failures means *this host is offline* or just *Anthropic
is having a moment*, and it was called straight from the request coroutine. So every failed turn
froze the router for the length of that probe, for every other session on it, at exactly the moment
the router is busiest.

Moving it to a worker thread fixes the stall and exposes a second fault underneath. A probe started
by one request can finish after a NEWER request has already succeeded and closed the breaker, and it
then writes its stale verdict over that success. The window is the probe's own duration, which on a
degraded link is seconds.

So one lock serialises every breaker verdict, failure and success alike, and the breaker sees them in
the order the requests actually resolved. Cancellation gets its own rule: cancelling an `await` cannot
stop the worker thread it is waiting on, so a cancelled caller keeps the lock until its probe finishes.
Releasing early would let that thread mutate the breaker behind whoever took the lock next.

| | |
|---|---|
| Probe | Runs in a worker thread, never on the event loop |
| Ordering | One `asyncio.Lock` over failure and success, so a slow probe cannot overwrite a newer success |
| Cancellation | The lock is held until the orphaned probe thread finishes |

`DEFAULT_FAILOVER_THRESHOLD` replaces the bare `1` that `Settings.failover_threshold` and
`FailoverBreaker.__init__` each carried, with a comment on each asking the other to stay in step.


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

**Backdoor retries one pre-send Anthropic transport failure before surfacing it.** A connect or
pool failure means nothing reached Anthropic, so a second attempt cannot duplicate anything. The
router retries those once on the same pool, then lets the circuit breaker decide whether the host
is offline. Pool exhaustion uses a fresh pool as described below. Read, write and protocol errors
are never replayed: the request may already be on Anthropic's side, and a blind retry would bill
and run the turn twice.

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
the file. Applying a changed launch agent is a live control-plane operation. Do it only from an
independent Terminal session with direct-cloud rescue access and a rollback copy already in place.
After the user-operated change, inspect the applied limit with:

```bash
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

#### What the proxy log tells you, and what it used to bury

Three things can end an intercepted connection early, and `_intercept` caught all three in one clause and logged them under one message: `TLS interception for api.anthropic.com ended early`. The comment above it blamed a client that does not trust the router CA. Count them over the 8 days ending 2026-09-03 and that story falls apart:

| Exception | Occurrences | What it means |
|---|---|---|
| `ConnectionResetError` | 33,092 | The client dropped a pooled tunnel with an RST |
| `TimeoutError` | 465 | The client took the `200` and never sent a ClientHello |
| `ssl.SSLError: TLSV1_ALERT_UNKNOWN_CA` | 25 | The client does not trust the router CA |

Claude Code and Codex both pool their sockets, so the first two rows are pool churn and neither is a fault. They also cost about a third of a 10 MB rotation, and they buried the 25 lines that matter. A client hitting that third row cannot reach the router at all, which is worth reading on the day it happens rather than after `grep -c`.

Each one now gets its own treatment:

| | |
|---|---|
| CA distrust | WARNING every time, naming the host and the path to the CA the client needs to trust |
| Dropped before TLS | Counted, then one INFO line per 5 minutes with the total and the split between resets and handshake timeouts |
| Failure after the splice starts | WARNING, because `_pipe` swallows all three and `_splice` gathers with `return_exceptions=True`, so this should be unreachable. If it ever fires, that is news |

There is no per-occurrence line for the dropped tunnels, DEBUG included. `configure_logging` puts the root logger at DEBUG, so a debug call would write all 33,557 of them to the same file under a quieter label.


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

Detached rather than a checked-out branch, so `git checkout main` still works in the dev tree.
Symlink the config instead of copying it, so the two never drift apart. Keep deployment separate
from source work: an agent can prepare and verify the candidate commit, but only the user changes
the detached live checkout or restarts the launchd job. Agents can inspect live state with:

```bash
git -C ../backdoor-service status --short --branch
git -C ../backdoor-service log -1 --oneline
launchctl print gui/$(id -u)/com.screddy.backdoor-router | head
```

The 2026-08-31 incident explains this boundary in
[`docs/incidents/2026-08-31-router-self-lockout.md`](docs/incidents/2026-08-31-router-self-lockout.md).

The tradeoff is that editing code in your clone no longer deploys. That is the point: a restart
can no longer pick up half-finished work. The example LaunchAgent writes the rotating router log
to `~/Library/Logs/backdoor-router.log`.

One launchd wrinkle if you script that swap: `launchctl bootout` returns before teardown finishes, so an immediate `bootstrap` fails with `Bootstrap failed: 5: Input/output error`. Poll `launchctl print gui/$(id -u)/<label>` until it errors, then bootstrap.

### Bare mode: what the local model actually receives

A failed-over request does not reach the local model as Claude Code sent it. Backdoor strips the harness off first, and this is the change that decides which model you can afford to run.

Measure an ordinary session and the reason becomes obvious. With the usual MCP servers attached, the system prompt and tool definitions alone came to roughly 286K tokens on this machine, before a word of conversation. Two model generations of this failover ladder were shrunk to cope with that number: the tier went 9B down to 4B in July 2025 after a 9B spent about five minutes per turn prefilling a 186K-token session and then returned a 500. The context was treated as fixed and the model kept getting smaller.

Bare mode attacks the other term. Before the request goes to Ollama, Backdoor drops the system prompt (replacing it with a few sentences of orientation — which sentences depends on the path, see [Two paths, two replacement prompts](#two-paths-two-replacement-prompts)), drops tool definitions, truncates tool results in the transcript to a character budget, and replaces images with a placeholder. Your conversation stays. On a representative request that arrived carrying a full harness and an 80KB file read:

```
50,439 tokens  ->  1,196 tokens        (42x)
answer in 3.6s on qwen3.5:27b, ending in a real tool call
```

That is what lets the default tier be a 27B rather than a 4B without repeating the prefill regression. The two changes belong together: switch bare mode off and leave the 27B in place, and you rebuild the original failure on a much larger model.

**Built-in tools survive; MCP tools do not.** `Read`, `Edit`, `Bash`, `Glob`, `Grep`, `WebSearch`, and `WebFetch` are Claude Code tools rather than MCP integrations, so bare mode keeps them. A deliberate local Qwen session can search or fetch when the Mac has internet access, and Bash can call public HTTP APIs with `curl`. If a network call fails, Qwen continues with local tools. True breaker failover gets an explicit offline prompt because the breaker opens only after the connectivity probe confirms that the host cannot reach the internet.

Every `mcp__*` tool is dropped from bare requests. Those schemas supplied most of the measured ~286K-token tool payload, and remote MCP integrations cannot help during true offline failover. Keep a specific MCP tool only through the existing allowlist when its schema cost and availability justify it.

The standalone wrapper attaches MCP servers per request instead of loading the whole inventory:

```bash
qwen mcp list
qwen mcp screddy-hermes -p "check the requested conversation"
qwen mcp composio-tmn,atlassian
```

`qwen mcp NAME` validates each name against `~/.claude.json`, runs the same certificate-verifying internet probe as the hybrid router, and writes a private session config under `~/.cache/backdoor/`. Only the named servers start. Memory stays independent of this switch because Backdoor reads the local claude-mem replica. If the Mac is offline, Qwen skips the requested MCP connection and keeps working with local tools. `QWEN_MCP_ASSUME_ONLINE=1` bypasses the probe on a network that blocks its public endpoints while allowing the selected MCP.

**The tier must accept tool definitions.** This is a hard pairing, not a preference. `deepseek-r1` at any runnable size does not: Ollama answers a request carrying tools with `does not support tools`, HTTP 400, killing the session failover exists to save. If you swap the tier for a model without tool support, set `failover_keep_tools=""` at the same time.

### Deliberate routing: `/model qwen`

Failover is not the only way into a local tier. A session can ask for one by name with `/model qwen`, and that request takes a different branch. It matches `MODEL_ROUTES`, returns a profile immediately, and skips the failover block underneath. Only the failover block stripped.

The deliberate route receives an online-capable lean prompt. It tells Qwen to use `WebSearch`, `WebFetch`, or `Bash` with `curl` when current information matters, then continue offline if the call fails. This keeps inference local while letting the agent use the network that is already available to Claude Code's built-in tools.

So `/model qwen` handed the 27B a full harness session against a 32K window, which is the pairing the section above warns about. That window is small on purpose. Bare mode is what makes it generous.

Profiles whose window assumes bare mode now say so:

```
ROUTE_BARE=true
```

Set it only on profiles whose windows assume a stripped prompt. The 64K
`qwen-fast` route retains its system prompt and tools.

| `/model` name | Profile | Model | Window | Stripped |
|---|---|---|---|---|
| `qwen` | `local-qwen38-obliterated` | Qwen3.8-27B OBLITERATED Q4_K_M (GGUF) | 32K | yes |
| `qwen38-obliterated` | `local-qwen38-obliterated` | the same tier, named directly | 32K | yes |
| `qwen38-action` | `local-qwen38-action` | action-tuned MLX rollback | 64K | yes |
| `qwen-fast` | `local-fast` | `qwen3.5:4b-64k` | 64K | no |

The `qwen-9b` and `qwen-stock` routes were removed with the local Qwen 3.5 9B
artifacts. The terminal `fusion-qwen` agent now uses `qwen-fast` for its local
orchestration step; the verifier council keeps its separate models.

Stripping reuses the `failover_*` keep-list and truncation budget, so both paths build the same request shape. A route that stripped differently from failover would be a second behaviour to keep in sync for no gain.

#### Two paths, two replacement prompts

Same strip, different standing text — because the situations are not the same. `make_bare` replaces `system` **wholesale**, so whatever it substitutes is the only orientation the model gets.

| Path | Constant | What it says |
|---|---|---|
| Failover (breaker open) | `DEFAULT_SYSTEM` | the session has lost its network — which is true, that is why the breaker opened |
| `/model <name>` + `ROUTE_BARE` | `ROUTE_SYSTEM` | nothing failed, the switch was deliberate; states the 32K window and the reduced tool set |

The route path used to borrow the failover text, which told a healthy session it was mid-outage. A model that believes it is offline hedges and declines work it can do, and the claim is simply false on a path nobody failed over on.

Wholesale replacement had a second cost that was easier to miss. The `qwen` wrapper injects the repo's PR rules with `--append-system-prompt`, precisely because `--bare` skips `CLAUDE.md`. Those rules live in `system` — so `ROUTE_BARE` deleted them, and a routed session never saw the every-branch-gets-a-PR rule at all. The route path now appends them back:

```
ROUTE_SYSTEM_FILE=prompts/qwen-pr-rules.md   # default; empty disables
```

Relative paths resolve against the repo cwd, the same way `profiles/` does. The file is read through an `lru_cache`, so it costs one stat per process, and a missing or unreadable file is deliberately **not** fatal — the route still works carrying `ROUTE_SYSTEM` alone. Failing a request over a documentation file would be the worse outcome.

Budget: `ROUTE_SYSTEM` plus the default rules file composes to ~2.8KB, about 700 tokens, roughly 2% of the 32K window. That is the price of a routed session that knows the rules it is supposed to follow.

The failover path is untouched and keeps `DEFAULT_SYSTEM`; `test_failover_keeps_the_outage_prompt` pins that so this change cannot leak across.

#### Stripping bounds the prompt, not the transcript

**Profile mode strips too, and did not until 2026-09-03.** `bd switch ...; bd claude` runs the router in profile mode, which translates every request to the single active profile and never enters the `MODEL_ROUTES` branch at all — so nothing had honored `ROUTE_BARE` on that path. The `qwen` wrapper launches Claude Code with `--bare`, which hid the gap: a caller who skipped the wrapper reached the local tier with the full harness attached, at a 32K window.

It strips with `ROUTE_SYSTEM` and the operator rules, exactly like the `/model` path, and only when nothing has already stripped the request — failover replaces the prompt with `OFFLINE_SYSTEM`, and re-stripping there would tell an offline model the network is fine.

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
| Codex through Backdoor | advertises 32K, compacts at 27,904 total tokens, and keeps a 4K local reply reserve; this path ships in the Codex failover change |

The mid-session Claude path cannot change its process environment after `/model` runs. Its router guard supplies the hard boundary. Known Claude model IDs keep their native compaction policy, and cloud output remains uncapped.

The backend must also return usable summary text. On 2026-08-28 the action-tuned MLX tier reached Claude Code's client limit at 32K. The compact request itself was small: 994 backend tokens. MLX generated nine tokens, all inside its inline thinking block. `PROVIDER_STRIP_INLINE_THINKING=true` removed those tokens and Claude Code received an empty summary twice. The default route now uses the OBLITERATED Q4_K_M GGUF. Its OpenAI endpoint returned an ordinary answer alongside internal reasoning in live compaction testing. The MLX checkpoint remains available as `qwen38-action` for measured action-contract work.

#### Memory is the other half of a small window

A short window works only when durable facts live outside the prompt. Backdoor reads the local claude-mem SQLite replica before each local turn and injects a bounded result as background context. This adds no MCP schema and still works when the network is down. `QWEN_MEMORY=0` disables recall.

Large fetched pages also bypass the model window at the proxy layer. Once Claude, Codex, or another client returns a page as a tool result, Backdoor replaces results over 12,000 characters with up to 6,000 characters of passages ranked against the current question. Durable storage is off by default. `EXTERNAL_CONTEXT_PUBLIC_URL_PREFIXES` accepts comma-separated, reviewed public URL prefixes whose unauthenticated fetch output may be submitted to the local claude-mem worker. Browser-session tools never persist their output.

Backdoor does not fetch arbitrary URLs itself; the client remains responsible for browsing and authentication. Unapproved, authenticated, intranet, and client sources remain ephemeral. Approved source text is marked as untrusted data, high-confidence credential-shaped documents are not written, and stored source URLs exclude user information, query strings, and fragments. Individual documents are capped at 500,000 characters before ranking, and each request can enqueue at most four documents. Worker calls time out after 1.5 seconds and fail open, while the local 6,000-character reduction still protects Qwen's window.

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

`PROVIDER_KEEP_ALIVE` does two opposite jobs, and which one depends on who asked for the tier.

On `local-failover-256k` and `local-failover-128k` it is `45s`, to release EARLY: nobody asked for those tiers, and an outage that ends with its sessions abandoned leaves no successful call to close the breaker.

On `local-qwen38-obliterated` it is `10m`, to release LATE. The rule here used to be "never set this on a tier reachable through `MODEL_ROUTES`", because a short clamp would make a `/model qwen` session evict its own 17 GB model while the user was still thinking. That reasoning was right about short clamps and it argues for a long one: the global `OLLAMA_KEEP_ALIVE` is five minutes, and the working set only pays while Ollama still holds the prefix, so any pause longer than five minutes silently costs a 70-100s cold prefill at an 18K window. Ten minutes rather than thirty because a resident 27B is 17 GB and every minute of it is a minute `llmjury solve` can collide with, Ollama capping by model count and never by bytes.

Only breaker-diverted requests are CLAIMED, which is a different thing: a claim is released when the breaker closes, and the user asked for a routed tier, so it is not ours to evict on someone else's schedule. Escalation is the one exception, above.

#### Escalating frees the tier it leaves

The rule above says a tier the user deliberately asked for is not ours to evict. Escalation is the one case where it stops applying, because the session has already stopped using it. A `/model qwen` session that outgrows the 27B is moved to `qwen3.5:4b-256k` by `ROUTE ESCALATE` (hybrid) or `TIER ESCALATE` (profile mode, and the universal backstop). The user still asked for the 27B, but nothing is being served from it any more.

Until 2026-09-05 nothing freed it. A route holds no breaker claim, so no close would ever release it, and Ollama keeps a model for the global `OLLAMA_KEEP_ALIVE` after its last request — so the two tiers overlap for five minutes by design.

They do not fit. Measured 2026-09-05 with the 27B alone resident: **22.0 GB wired** of a ~27 GB ceiling on 36 GB of RAM. The 256K 4B wants roughly 13 GB more, and `OLLAMA_MAX_LOADED_MODELS=3` entitles Ollama to try holding both, because it caps by model count and never by bytes. `mlx_admin.resolve_profile` has guarded the MLX-versus-Ollama form of this collision since 2026-08-24, citing the two kernel panics; the Ollama-to-Ollama form had no equivalent.

Since 2026-09-05 there is a byte-level guard at the one place every Ollama client shares. The scheduler trusts Metal's static 28.1 GiB budget on this host and ignores system free: at 15:56 that day it logged `free="8.9 GiB" free_swap="0 B"` and loaded a 16.5 GB model on top of a partly resident llm-jury council. `~/Library/LaunchAgents/com.screddy.ollama.plist` now sets `OLLAMA_GPU_OVERHEAD=4GiB`, so Ollama budgets 28.1 minus 4.0 minus the 0.5 GiB minimum, 23.6 GiB. Verified after the restart: the 27B still loads at its full 32K window (`context_length=32768`, 17.6 GB projected), the current council's measured top of 23.4 GiB still fits, and the two no longer stack; Ollama evicts the resident one instead. Peak wired on the host drops from roughly 34 GB to roughly 30 GB. llm-jury's memguard default moved from 0.70 to 0.65 in llm-jury #31 so the preflight refuses the same runs the server would evict. `OLLAMA_MAX_LOADED_MODELS` stays at 3 because the council is three models; the guard is the bytes, not the count.

Both escalation sites now unload the outgoing model. The safety rule is the same one the 2026-08-26 stall bought: never evict a tier that is still generating. The in-flight count therefore covers **every** locally served response, streaming and not, failover and deliberate route — a route response holds no claim but does hold the GPU. When something is still open the eviction is deferred and runs as the last response finishes. Failing to evict is logged and never costs the request; the fallback is the old behaviour, one tier resident too long.

Everything here is best-effort. A router that cannot reach Ollama's admin endpoint must still route, and the cost of failing is late release, which is the old behaviour rather than an outage.

### The prefix is the budget, not the window

Measured on this host, 2026-09-05, against `qwen3.8:27b-obliterated` at its 32K window:

| | |
|---|---|
| Cold prefill 6,840 tokens | 26.3s (260 tok/s) |
| Cold prefill 12,815 tokens | 49.1s (261 tok/s) |
| Cold prefill 27,008 tokens | 135.6s (199 tok/s) |
| Append ~800 tokens to a transcript the model just saw | **5–10s** |
| Byte-identical repeat | 0.7s |
| Decode | 8.9 tok/s |

And against `qwen3.5:4b-256k` (8.8 GB resident at `num_ctx 262144`, not the ~13 GB estimated before it was measured):

| Cold prefill 73,150 tokens | 181.9s |
|---|---|
| Cold prefill 103,277 tokens | 391.2s |
| Append after either | 1.6–2.4s |

The expensive event is not the size of a conversation. It is showing the model a prompt whose prefix it has not already processed. Ollama reuses the KV cache for a shared prefix, so an ordinary turn costs seconds while the same transcript presented cold costs minutes. Everything below follows from that one fact.

**Trimming beats escalating.** The 2026-09-04 session that showed 100% context and appeared frozen was about 142K tokens on the 256K tier — roughly nine minutes of prefill on the curve above. Bounding the same session to 18K and keeping the 27B costs about 70 seconds, once, and leaves the stronger model answering. `LOCAL_WORKING_SET` is therefore tried before `FAILOVER_LADDER`, and the ladder becomes the fallback for a request that cannot be trimmed under the ceiling — one message larger than the ceiling has nothing to drop.

**The boundary is sticky, and that is the whole design.** A window recomputed on every request moves the prefix on every request, which converts a 5–10s append into a 40–50s cold prefill — a context manager that costs five times what it saves. So the boundary is chosen when `LOCAL_WORKING_SET_MAX_TOKENS` is crossed and then reused unchanged for as long as the conversation keeps fitting under it. One turn per cycle pays a rebuild; every other turn appends.

**Model-written compaction was rejected on the same numbers.** At 8.9 tok/s a 1,500-token summary costs 169s and a 2,500-token one 281s, and the turn after it is a cold prefill of whatever was produced — 3.5 to 5.5 minutes per compaction, recurring every 6–15 turns at typical tool-result sizes. Deterministic selection costs no model call at all. Claude Code's own auto-compaction still runs client-side against the window the launcher declares; nothing here replaces it, and bounding makes it fire later by sending less.

**One local inference at a time per tier.** Two ~45,600-token sessions alternating on one model measured 105.8s and 116.3s per turn, against 0.7s for a single session repeating: each request evicted the other's KV cache, so every turn became a cold prefill. `OLLAMA_NUM_PARALLEL=2` provides two slots but not two retained prefixes at these sizes. Interleaving costs both sides roughly 100x while queueing costs the second session one turn of waiting, so local inference is serialized per `(base_url, model)`. Different tiers still run together. Waiting past `LOCAL_TIER_LOCK_TIMEOUT_SECONDS` proceeds unlocked rather than failing the request.

Measured end to end on 2026-09-05, driving a dev router on port 8099 against the real 27B with a 205,400-token, 260-message session:

```
⇢ WORKING SET [qwen3.8:27b-obliterated] kept 21/260 messages in≈17734 (rebuilt)
⇢ WORKING SET [qwen3.8:27b-obliterated] kept 23/262 messages in≈17771 (stable)
⇢ WORKING SET [qwen3.8:27b-obliterated] kept 25/264 messages in≈17808 (stable)
```

First answer in 116s, the turn after it 143s — the second cold prefill, since the first request after a model load leaves no reusable cache — then 10.6s, 13.3s and 13.8s as the boundary held and each turn appended. The same session before this change escalated to the 256K 4B and prefilled all 205K tokens, which on the measured curve is about thirteen minutes to first answer, on a weaker model, every time the session was resumed.

#### Codex replays a stable prefix too

Codex sent the ACTIVE TURN ALONE — the latest instruction, its paired tool calls, and recalled memories — and discarded the rest. That keeps the prompt small and guarantees a cold prefill on every turn, because the head changes with every new instruction. Real turns from this machine's log, all of them small:

```
  2648 tok    232.5s     11 tok/s
  2578 tok    220.4s     12 tok/s
  6643 tok    223.6s     30 tok/s
  3354 tok     20.5s    163 tok/s
  6702 tok     43.9s    153 tok/s
  2556 tok      1.3s   1925 tok/s
```

One turn in that sample reused a prefix and cost 1.3 seconds. The rest ran at cold-prefill rate or worse — a 180x spread across nearly identical inputs. The tier lock removes the contention half of it. The structural half is fixed by replaying history: `CODEX_HISTORY_BUDGET_TOKENS` (default 12,000) of earlier items go ahead of the memory block, behind the same sticky boundary the Claude path uses, so turn N's payload opens with turn N-1's.

Three things make that safe to send where the active turn alone was safe by construction:

- **An allowlist, not a denylist.** Only user and assistant messages and paired `function_call`/`function_call_output` items are replayable. `additional_tools` advertises hosted web search, developer messages restate a cloud harness the local preamble already replaced, and reasoning items carry `encrypted_content` only the cloud can verify.
- **The budget is leftovers.** History spends what the active turn did not use, capped — never its own allocation. A large instruction reduces history to nothing and the path behaves exactly as it did before, which is why `validate_codex_context_allocation` did not have to move.
- **Pairs stay whole.** A window never opens on a `function_call_output` whose call was left behind.

Validated against real Ollama on 2026-09-05, posting the built payload to the same `/v1/responses` endpoint the Codex path uses, with a 10-turn conversation:

```
turn 1: items=23  in≈4316   34.6s  status=completed
turn 2: items=25  in≈4367    4.0s  status=completed  prefix_stable=True
```

Turn two is the same size and 8.6x faster, and the provider accepts the replayed `function_call` items rather than rejecting the shape — the two things that could not be proven from unit tests alone. Before this change every turn cost what turn one costs.

Memories sit between the history and the active turn on purpose: the block is rebuilt from a fresh recall query every turn, so anything placed after it is new work anyway, and putting it ahead of the history would make the history unreusable.

#### The provider counts differently, so measure it

The router sizes a request with tiktoken; Ollama prices the rendered chat template. Every local response now logs both — `prompt: 17677 provider tokens for an estimate of 19905 (ratio 0.89)` — and warns when a prompt reaches the window, because Ollama truncates from the front silently and the symptom is a model that answers nothing.

Measured across a real routed Claude Code session on 2026-09-05, the ratio sat at **0.89–0.90** on ordinary traffic and fell to 0.18–0.41 when a single enormous message dominated. The router over-counts, mildly. `LOCAL_TOKEN_ESTIMATE_RATIO` therefore defaults to **1.0**, and `PROVIDER_CONTEXT_TOKENS` records each tag's real `num_ctx` so `_window_guard` can cap the working set at `(window − reply reserve − template slack) ÷ ratio`.

This setting shipped at 1.8 for one revision, on the belief the provider counted more. That number came from comparing two different requests, and the session that caught it is worth keeping: at 1.8 the guard is 15,095, which is **below** the ~19K that the system block and tool schemas cost on their own, so the working set could never fit inside it and five turns in a row logged `cannot reach 15095 tokens (tail alone is 19877)` and fell through to the ladder. A guard smaller than a request's irreducible overhead is not a guard, it is an off switch. Raise the ratio only from logged pairs.

**A harness note worth more than it looks.** Driving a dev router with `set -a; . profiles/<tier>.env` exports `PROVIDER_MODEL` into the process, and process env beats `env_file` in pydantic-settings — so every ESCALATED profile silently resolves back to that model, and a run that logs `ROUTE ESCALATE [... → local-failover-256k]` is still served by the 27B. Export only the router-level knobs when testing escalation. The live router carries no `PROVIDER_*` in its environment, so this is a test artifact rather than a deployed one; `ps -wwE` on the running process is how to confirm that.

| Setting | Default | What it does |
|---|---|---|
| `LOCAL_WORKING_SET` | `true` | Bound the transcript for local tiers |
| `LOCAL_WORKING_SET_TARGET_TOKENS` | `18000` | What a rebuild aims for |
| `LOCAL_WORKING_SET_MAX_TOKENS` | `22000` | What triggers one |
| `LOCAL_TIER_LOCK_TIMEOUT_SECONDS` | `900` | How long a second session waits before proceeding unlocked |
| `PROVIDER_CONTEXT_TOKENS` | per tag | The tier's real `num_ctx`, which the window guard sizes against |
| `LOCAL_TOKEN_ESTIMATE_RATIO` | `1.0` | Provider tokens per estimated token; raise only from logged pairs |

Bounding applies only where the provider is a local Ollama tier. The ceiling is a property of this machine's GPU, so applying it to a hosted provider would discard context for nothing. The client keeps its full transcript either way — this decides what is forwarded, exactly as bare mode decides how much of each message is forwarded.

### Recall shares its budget instead of spending it first-come (2026-09-04)

Two duplicate cases survived that change and were fixed the same day. The loop compared each
original against `seen` but added the clipped text to it, so any memory long enough to clip never
matched a later copy of itself; claude-mem writes near-identical summaries across sessions, so the
same lesson landed twice and took two shares. Separately, two distinct memories can share a head
longer than their share of the budget, which reached the reader as the same line twice. `seen` now
holds originals and a second set holds what was emitted, so both cases return once. Two tests pin
each case; both failed against the previous loop before the fix went in.

`memory.recall()` fills a character budget from the best-ranked memories. Two things were wrong,
and together they meant the router recalled nothing.

**It stopped at the first memory that did not fit.** The loop `break`ed rather than skipping, so
one long memory discarded every shorter one ranked behind it. That is not a corner case: a
claude-mem session summary runs 700-1400 characters while bare mode allows 1200 in total.
Measured against the real store on this machine, `corpus ingest cognee rename`,
`qwen router failover` and `claude-mem sync hub` each returned **zero** memories. The search was
fine — 4, 13 and 7 rows matched and joined — and every row was then thrown away.

**First-come selection cannot fill six slots from a 1200-character budget anyway.** The budget is
deliberately small: bare mode exists to hold the prompt near 945 tokens against a 32K window. But
one summary is larger than a sixth of it, so even with the `break` fixed a single memory spent the
lot and five slots went unused.

Each memory now takes a share of what is left, divided between the memories that can still land,
and is clipped to that share on a word boundary. Clipping is safe because the columns are selected
lesson-first — `learned` before `investigated` and `request`, `title` before `narrative` — so the
head of the text is the part worth keeping. Room that the remaining slots cannot use flows to the
memories that exist, so two candidates against an 8000-character budget are taken whole rather
than clipped to an eighth.

| | before | after |
|---|---|---|
| qwen bare mode (k=6, 1200 chars) | 0 memories | 6 memories, 1197 chars |
| Codex failover (k=8, 8000 chars) | 0 memories | 8 memories, 7982 chars |

**Matched-but-dropped is no longer silent.** Recall is fail-open, so a broken filter and a store
with nothing to say both returned an empty list — which is why this survived. When candidates
match but none fit, recall now logs a warning naming the count, the budget and `k`, and says
recall is off.

**Left alone deliberately:** `_SOURCES` still includes `user_prompts_fts`. Prompts are 32% of rows
on this store and rank first for several ordinary queries, so they take slots a distilled summary
could have used. Whether the router should recall verbatim prompts at all is a product decision,
not a defect.

### Sizing the failover tier

Ollama caps residency by model count, never by bytes, and Metal allocations are wired and cannot be paged out. Over-commit and this host panics instead of raising OOM. Measure with `ollama ps`, which reports resident size; `ollama list` reports on-disk size and will mislead you by roughly half.

On a 36GB M5 Max at `OLLAMA_NUM_PARALLEL=2`, flash attention on, `q8_0` KV cache:

| Tier | Params | On disk | Resident | Tools | Notes |
|---|---|---|---|---|---|
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

From 2026-08-25 through 2026-08-28 it backed `/model qwen`, the wrapper's lean mode, and cloud-to-local failover. The empty compaction response moved those unattended paths to the GGUF tier. `qwen38-action` still names this checkpoint directly.

Read the model card before you lean on it. Reduced refusal is not permission, and it says so itself: the card puts unsupervised execution with destructive, financial, credential, or otherwise high-impact tools out of scope, and this wiring puts the model on exactly those paths. Failover fires with nobody watching, and `local-worker` gets dispatched with Bash and Write. Scoped tool permissions and reading what an unattended agent actually did are the controls now, because the model is no longer one of them.

#### It is the one tier nothing loads lazily

Every other local tier is an Ollama tag that loads on first request and gets evicted on a timer. This one is a launchd job holding about 19GB, up or absent, with nothing in between. Pointing failover at a tier that cannot start itself would break the fallback in the one situation it exists for, so `src/proxy/mlx_admin.py` probes `127.0.0.1:8080/health`, runs `launchctl kickstart` when it finds nothing, and waits up to 90 seconds for the weights to load. When the server will not come up, the request goes to `local-fast`:

```
⇢ MLX FALLBACK [local-qwen38-action → local-fast] /v1/messages
```

The 4B fast tier loads through Ollama on demand and leaves enough memory
headroom for recovery after an MLX startup failure. It took over from
`local-failover-heavy`, the 9B profile removed on 2026-08-31 (#70); the comment
in `profiles/local-qwen38-action.env` still named that dead profile until
2026-09-05, which is the kind of drift that makes an operator expect a fallback
tier this host cannot load.

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

Local sessions read the synced claude-mem replica at `~/.claude-mem/claude-mem.db` and prepend bounded recall to the system prompt as plain text. The read needs no MCP server, tool call, or network.

Claude's lifecycle hooks normally supply memory, but the `qwen` wrapper's lean and fast modes pass `--bare`, which disables every hook. Backdoor therefore injects recall at the proxy layer, the one path every local request crosses.

| Path | Before | Now |
|---|---|---|
| `/model qwen` (router) | path-dependent | proxy injects |
| Cloud→local failover | path-dependent | proxy injects |
| `qwen full` | hook injects | proxy deduplicates |
| `qwen`, `qwen lean` | **nothing** | proxy injects |
| `qwen fast` | **nothing** | proxy injects |

```
PROVIDER_BASE_URL=http://localhost:11434/v1   # injection only fires for local providers
MEMORY_INJECT=false                           # turn it off
MEMORY_TOP_K=6
MEMORY_CHAR_BUDGET=1200
```

Cloud providers are excluded so a session whose hook already ran does not pay for the same text twice. Recall is read-only (`mode=ro`), fails open on a missing or locked database, and times out in 1.5 seconds so the sync worker's write lock cannot stall a turn. The budget stays small because bare mode exists to hold the prompt near 945 tokens.

Memories are labelled as background that may be stale rather than as instructions, because they are. The mirror still describes the heavy tier as `qwen3.5:27b-bare` at 15 GB, two facts that are both now wrong, and a local model asked about the tier will say so rather than assert the stale version.

### Building a bare tag

Build from the **registry** tag, never from a local one. `modelfiles/build.sh` appends a ~43K-token system prompt to every `*.Modelfile` it builds and defaults to building all of them, so every local `qwen3.5:*` tag carries 186,647 bytes of baked prompt. `FROM` inherits it, which makes a "bare" model that is nothing of the sort. The failure is not subtle but it is easy to miss: the first attempt here answered from the baked prompt's tool vocabulary and invented a `weather_fetch` tool the request never defined.

Bare Modelfiles therefore live in `modelfiles/bare/`, which a bare `./build.sh` cannot glob. Check any new tag with `ollama show --system <tag>`, which must return nothing.

#### Protect upstream base tags

`build.sh` derives its tag from the filename with the first `-` turned into `:`.
A file without a variant suffix can overwrite the matching upstream registry
tag with a prompt-baked manifest. The build script rejects those filenames.
Give persona builds a suffixed tag such as `*-fable`.

**`SYSTEM ""` does not undo it.** Rebuilding `FROM` a poisoned base with an empty `SYSTEM` still reports 186,647 bytes. The base itself has to be re-pulled.

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
| [Hermes delegate-only tier](docs/superpowers/plans/2026-09-05-hermes-delegate-only-tier.md) | 3 | A narrow machine-delegation surface for the bridge |

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
the repository. Successful password submissions use an HTTP 303 redirect to a same-origin
`/login/complete` GET. That GET loads a no-store approval page with a **Continue to Claude**
link. Loading a real page before the external callback keeps Chrome and Comet from treating the
callback as part of the password POST redirect chain and replaying the consumed login state.

**Delegate-only bridge.** A `delegate_only` profile exposes four run-scoped tools:
`hermes_chat`, `hermes_run_status`, `hermes_run_approve`, and `hermes_run_stop`.
The approval tool requires the request ID returned by `hermes_run_status` and maps its boolean MCP
input to the gateway's `once` or `deny` choice. A delayed caller cannot approve a newer action.
The bridge refuses that profile's session history, configuration, model, toolset,
job, log, status, and lifecycle tools before they reach the gateway or subprocess
runner. It also refuses caller-supplied `session_id` values, so a delegate cannot
attach a new run to an existing personal conversation. `hermes_list` remains
available for health discovery and returns only the profile's structured health
state and tier.

Run machine delegation through a second static-bearer bridge instance. Give it a
dedicated environment file, service name, bind port, bearer, and one-profile
registry. Keep the existing OAuth bridge and its broader registry unchanged. This
repository ships the tier, tests, and deployment examples. An operator must install
the second service, provide secrets, configure proxy routing, and restart it in a
separate deployment step.

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

For OAuth mode, proxy these paths to the same bridge process: `/mcp`, `/login`,
`/login/complete`, `/authorize`, `/token`, `/register`, and `/.well-known/oauth-*`. Claude's
custom connector needs only the public `https://.../mcp` URL; leave its advanced client
credential fields empty so Claude uses dynamic registration.

### Account-synced product connectors

`src/products_mcp/` runs three OAuth MCP instances for Claude desktop, web, and mobile:

| Connector | Owner | Public endpoint | Tools |
| --- | --- | --- | --- |
| HyperCrawl | Team Nebula | `https://hypercrawl-mcp.5-161-126-205.sslip.io/mcp` | `hypercrawl_status`, `hypercrawl_list_tools`, `hypercrawl_call` |
| HyperScale | Team Nebula | `https://hyperscale-mcp.5-161-126-205.sslip.io/mcp` | `hyperscale_status`, `hyperscale_list_tools`, `hyperscale_call` |
| EngageMate | Shawn Reddy Consulting (SRC), Screddyice GitHub organization | `https://engagemate-mcp.5-161-126-205.sslip.io/mcp` | `engagemate_status`, `engagemate_list_tools`, `engagemate_call` |

Each server advertises product-specific instructions during MCP initialization so clients can
route requests without relying on the connector name alone:

| Request intent | Connector | Exclusions |
| --- | --- | --- |
| Public web research, site search, crawling, URL mapping, page extraction, browser sessions, or structured website data | HyperCrawl | LinkedIn outreach and Instagram engagement |
| LinkedIn prospects, connections, outbound campaigns, sequences, outreach status, or templates | HyperScale | General web crawling and Instagram engagement |
| Instagram onboarding, audience discovery, engagement settings, account status, or service health | EngageMate | LinkedIn outreach and general web crawling |

The instructions direct clients to call the product's read-only status or list tool first. Clients
reserve account connections, campaign launches, messages, form submissions, and social engagement
for an explicit user request.

Set `PRODUCTS_MCP_PRODUCT` to one product for each process. The server refuses a missing or unknown
value and registers only that product's three tools. The list tool returns upstream operation names
and schemas. The call tool accepts only an advertised operation, which prevents callers from using
the bridge as an open HTTP proxy.

The bridge reuses `src/hermes_mcp/oauth.py` for dynamic client registration, PKCE, one-hour access
tokens, and rotating refresh tokens. It keeps product authorization separate behind the bridge:
HyperCrawl uses its tenant REST token, HyperScale uses its organization API key, and EngageMate
uses its internal key plus explicit user ID. Credentials stay in their existing protected
environment files. The Claude connector receives only the public MCP URL.

Deploy this branch in `~/backdoor-products-mcp`, then install
[`deploy/products-mcp-http@.service`](deploy/products-mcp-http@.service) as a user service. Start
the `hypercrawl`, `hyperscale`, and `engagemate` instances. Each instance loads its matching
`deploy/products-mcp-<name>.env` file after the shared credential files, so its bind address, port,
OAuth issuer, state path, and product selection take precedence. The separate checkout keeps
connector updates from changing the live router or Hermes bridge.

Merge [`deploy/products-mcp.Caddyfile`](deploy/products-mcp.Caddyfile) into the host Caddyfile and
add each public `/mcp` URL as a separate Claude custom connector. For each endpoint, complete OAuth,
confirm three tools in `tools/list`, run the matching read-only `*_status` tool, and reload Claude to
confirm persistence. Keep both the bare hostname and its `:443` form in each
`HERMES_MCP_ALLOWED_HOSTS` value because Caddy may forward either form after TLS termination.

---

<div align="center">

**Star this if you think the best coding agent should work with any model.**

</div>

## Working in this repo

Python `>=3.11`, managed with **uv** (`uv.lock` is committed). pytest is configured
in `pyproject.toml`. There is no Node toolchain here.

```bash
uv sync                       # install
uv run pytest                 # full suite
uv run pytest tests/<file>    # one file
```

`.claude-harness/init.sh` is not tracked. The claude-harness plugin rewrites it from its
own template in whichever checkout it runs in, so tracking it left every checkout carrying a
permanent modification — and `scripts/deploy-router.sh` aborts at preflight on a dirty service
checkout, which made a plugin artifact able to block a deploy. It also kept reverting the
committed version to an older format. The plugin recreates the file on demand; nothing needs it
in a fresh clone.

Worktrees usually point `.venv` at the main checkout's rather than building their own.
`.gitignore` carries `.venv` without a trailing slash so those symlinks are ignored too;
`.venv/` matches directories only, and a symlink shows up as untracked in every
`git status` the worktree ever runs.

Running a test you **expect** to fail? Suppress the test names. A red run exits 0 and
gets recorded as a success, and test names are declarative sentences that the memory
distiller inverts into architectural "rules":

```bash
uv run pytest -q --tb=no tests/<file>::<test> 2>&1 | tail -1
echo "EXPECTED-RED: fails without the fix, as designed"
```

This repo is the **source**. The live control plane is operated by hand from an
independent Terminal session, and machine hooks reject agent attempts to touch it —
see `~/.claude/CLAUDE.md`, section "Backdoor live-control boundary". Agent
instructions live in `CLAUDE.md`.

## Memory: claude-mem replica (since 2026-09-04)

Every memory path in the router now reads the local claude-mem store,
`~/.claude-mem/claude-mem.db`, and nothing else. `src/proxy/memory.py` runs FTS5 queries
over its summaries, observations and prompts (read-only, 1.5 s timeout, fail-open),
`src/proxy/memory_recall.py` wraps that for Codex failover turns, and
`external_context.remember_document` posts fetched documents to the local worker on
`127.0.0.1:37701` as verbatim prompts, which the worker queues durably and syncs to the
cmem.ai hub. Cognee and Mem0 are gone: no tunnel, no API key, no HTTP recall.
The superseded Cognee failover design and implementation plan were removed from `docs/` so no
operator can mistake them for a supported recovery path; Git history retains the old design.

Settings renamed with it: `codex_memory_timeout_seconds`, `codex_memory_top_k`,
`codex_memory_char_budget`, `qwen_memory` (env `QWEN_MEMORY`), plus
`memory_db_path` and `memory_worker_url`. The Qwen launcher no longer builds or
attaches a memory MCP shim.

The store is a synced replica of every device, so a failed-over local session recalls
what Hermes on src or r2h learned, even when the network is down.

## CI (2026-09-05)

`.github/workflows/ci.yml` runs the suite on every pull request and on pushes to `main`.

This repo had no CI at all. Shawn's QA Assist reviewed its pull requests and then declined to
merge them, reporting "this repo has no gate I can trust" and asking whether it should merge on
review alone. That was the right refusal: a review with nothing runnable behind it is an opinion,
and this repo is the router every local model session goes through.

The suite is the gate: failover, memory recall, the working set, tier serialization and the
provider edges. The count is deliberately not quoted here — it moves every time a regression gets
cover, and a number in prose only ever goes stale. Nothing here needs a secret, so the workflow runs with `contents: read` and no
environment.
