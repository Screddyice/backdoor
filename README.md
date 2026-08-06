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

## Troubleshooting

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

## Offline failover (hybrid mode)

In hybrid mode Backdoor passes Anthropic-bound traffic straight through to the real API and only steps in when it has to. A circuit breaker watches passthrough requests, and when it opens, `/v1/messages` is served by a local Ollama profile instead — so an in-flight session survives losing the network. The profile is chosen by session size, so a large context escalates to a wider-window tier rather than being truncated.

> **Point Claude Code at the router, or none of this runs.** Failover lives in the request path, so a session started with `ANTHROPIC_BASE_URL` unset gets a plain API error when the network drops. That is not a bug in the breaker; the breaker was never consulted. If an outage produced an error instead of a local answer, check the base URL first.

### Bare mode: what the local model actually receives

A failed-over request does not reach the local model as Claude Code sent it. Backdoor strips the harness off first, and this is the change that decides which model you can afford to run.

Measure an ordinary session and the reason becomes obvious. With the usual MCP servers attached, the system prompt and tool definitions alone came to roughly 286K tokens on this machine, before a word of conversation. Two model generations of this failover ladder were shrunk to cope with that number: the tier went 9B down to 4B in July 2025 after a 9B spent about five minutes per turn prefilling a 186K-token session and then returned a 500. The context was treated as fixed and the model kept getting smaller.

Bare mode attacks the other term. Before the request goes to Ollama, Backdoor drops the system prompt (replacing it with two sentences of orientation), drops tool definitions, truncates tool results in the transcript to a character budget, and replaces images with a placeholder. Your conversation stays. On a representative request that arrived carrying a full harness and an 80KB file read:

```
50,179 tokens  ->  1,096 tokens        (45x)
answer in 4.2s on deepseek-r1:14b
```

That is what lets the default tier be a 14B rather than a 4B without repeating the prefill regression. The two changes belong together: switch bare mode off and leave the 14B in place, and you rebuild the original failure on a larger model.

**No tools reach the failover model, on purpose.** Mem0 looks like the one worth keeping, and it is the wrong answer twice over. The breaker opens only when this host is offline, so a cloud-backed memory tool cannot work in the one situation where it would be called. And `deepseek-r1:14b` makes Ollama reject any request carrying tool definitions at all (`does not support tools`, HTTP 400), which kills the session failover exists to save. Local Mem0 recall still reaches the model, because the recall hook reads `~/.mem0-local/cache.db` client-side and injects memories into the prompt before the request leaves the machine. Bare mode keeps that text.

### Sizing the failover tier

Ollama caps residency by model count, never by bytes, and Metal allocations are wired and cannot be paged out. Over-commit and this host panics instead of raising OOM. Measure with `ollama ps`, which reports resident size; `ollama list` reports on-disk size and will mislead you by roughly half.

On a 36GB M5 Max at `OLLAMA_NUM_PARALLEL=2`, flash attention on, `q8_0` KV cache:

| Tier | On disk | Resident | Window | Notes |
|---|---|---|---|---|
| `deepseek-r1:14b-bare` | 9.0 GB | **20 GB** | 32K | Default. Arithmetic predicted 16GB; the compute graph accounts for the rest, which is why the number here is measured |
| `qwen3.5:4b-256k` | 3.4 GB | ~13 GB | 262K | Escape hatch for a transcript that overflows 32K |

The ceiling on this box is the 32B class at a 16K window, around 25GB, and it sits within about 2GB of what kernel-panicked this machine twice on 2026-07-31. A 70B is not an option: 42.5GB of weights exceeds total RAM. The 20GB default is safe because llm-jury stands down while failover is active, so nothing else is holding the GPU.

`deepseek-r1` is a reasoning model, so the profile sets `PROVIDER_REASONING_EFFORT=none` to suppress `<think>` traces. Treat that as load-bearing rather than cosmetic: those traces reintroduce the same latency the 9B was reverted for. Re-verify it after an Ollama upgrade.

**The breaker opens on exactly one condition: this machine is offline.** That is deliberately narrow, because failing over is not free — it loads a local model into Ollama, and on a machine that also runs a local council (see [llm-jury](https://github.com/Screddyice/llm-jury)) two GPU consumers at once will fight for memory.

So the trigger set is tight:

| Upstream behavior | Failover? | Why |
|---|---|---|
| Any HTTP response (`429`, `529`, `500`…) | **No** | A status code proves the request reached Anthropic and was answered. A usage limit is not a reachability problem, and hiding it behind a local model both masks a real signal and takes the GPU. The error is relayed so your client's own retry/backoff runs. |
| Transport error, host still online | **No** | Anthropic specifically is unreachable. Relayed verbatim so a provider outage stays visible. |
| Transport error, host offline | **Yes** | Nothing else is reachable either — local is the only way the session survives. |
| `401` / `403` | **No** | The network is fine and a credential is broken. Masking that would hide a revoked key indefinitely. |

Reaching the failure threshold is necessary but not sufficient: a TCP connectivity probe to a public address gets the final say, and it is re-taken each time (never cached), costing one probe per run of failures rather than one per request.

**Coordination with other local-GPU consumers.** Every breaker transition is published atomically to `~/.backdoor/failover-state.json`:

```json
{ "failover_active": true, "reason": "ConnectError", "updated_at": 1754130000.0, "pid": 4242 }
```

llm-jury reads that file and disables itself while failover is active, so the router and the council never contend for the same VRAM. Writing is best-effort — a router that cannot write the file still routes, and a missing or unreadable file reads as "not failing over".

| Setting | Default | Effect |
|---|---|---|
| `failover_to_local` | `true` | Master switch for hybrid-mode failover |
| `failover_bare` | `true` | Strip the harness off a failed-over request. Turn off only together with reverting the tier to a 4B |
| `failover_keep_tools` | *(empty)* | Comma-separated substrings of tool names to keep. Empty means no tools reach the local model |
| `failover_tool_result_chars` | `2000` | Per-tool-result character budget in the stripped transcript |
| `failover_threshold` | `3` | Consecutive transport errors before the connectivity probe runs |
| `failover_window_seconds` | `120` | Failures outside this window start a fresh run |
| `failover_probe_seconds` | `60` | How often an open breaker retries upstream (half-open) |
| `BACKDOOR_FAILOVER_STATUSES` | *(empty)* | Comma-separated HTTP statuses to restore as triggers, e.g. `429,529` |
| `BACKDOOR_FAILOVER_STATE` | `~/.backdoor/failover-state.json` | Where breaker state is published |

---

<div align="center">

**Star this if you think the best coding agent should work with any model.**

</div>
