```
██████╗  █████╗  ██████╗██╗  ██╗██████╗  ██████╗  ██████╗ ██████╗
██╔══██╗██╔══██╗██╔════╝██║ ██╔╝██╔══██╗██╔═══██╗██╔═══██╗██╔══██╗
██████╔╝███████║██║     █████╔╝ ██║  ██║██║   ██║██║   ██║██████╔╝
██╔══██╗██╔══██║██║     ██╔═██╗ ██║  ██║██║   ██║██║   ██║██╔══██╗
██████╔╝██║  ██║╚██████╗██║  ██╗██████╔╝╚██████╔╝╚██████╔╝██║  ██║
╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚═════╝  ╚═════╝  ╚═════╝ ╚═╝  ╚═╝
```
> Run Claude Code against any AI provider. DeepSeek, Groq, Ollama, OpenRouter — your call.

---

## Why would you use this?

**Claude Code is the best AI coding agent available.** The UX, the tool use, the agentic loops — nothing else comes close. But it's wired to Anthropic's API, which means every session costs real money.

This proxy breaks that dependency. You get the full Claude Code experience — same CLI, same tools, same workflow — while the actual inference runs on whatever backend you choose. Free tiers, cheaper APIs, local models, all of it.

| You want to... | How this helps |
|---|---|
| Try Claude Code without an Anthropic bill | Point it at NVIDIA NIM's free tier |
| Run it cheaper at scale | DeepSeek-V3 costs ~95% less than Claude Sonnet |
| Work fully offline | Route to a local Ollama or LM Studio instance |
| Benchmark models against real coding tasks | Swap `PROVIDER_MODEL` in one line, no other changes |
| One API key for 200+ models | Use OpenRouter as the backend |

---

## Before & After

**Before** — Claude Code, stock:
```
Claude Code CLI
       │
       ▼
 Anthropic API   ← $$$, rate limits, requires paid account
       │
       ▼
  Claude model
```

**After** — Claude Code through cc-nim-proxy:
```
Claude Code CLI   ← unchanged, same UX
       │
       ▼
 cc-nim-proxy     ← translates Anthropic → OpenAI format
       │           ← intercepts housekeeping calls (quota probes,
       │             title gen, etc.) to save quota
       ▼
 Any provider     ← DeepSeek, Groq, NVIDIA NIM, OpenRouter,
       │             Ollama, LM Studio, anything OpenAI-compatible
       ▼
  Response        ← translated back to Anthropic format
       │
       ▼
Claude Code CLI   ← sees a normal Claude response, none the wiser
```

---

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/ajsai47/cc-nim-proxy
cd cc-nim-proxy
uv sync

# 2. Configure your provider
cp .env.example .env
# edit .env — set PROVIDER_BASE_URL, PROVIDER_API_KEY, PROVIDER_MODEL

# 3. Launch (starts proxy + Claude Code in one command)
./run.sh
```

---

## Provider setup

Pick one. Change two lines in `.env`.

| Provider | `PROVIDER_BASE_URL` | `PROVIDER_MODEL` | Cost |
|---|---|---|---|
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | `meta/llama-3.3-70b-instruct` | Free tier |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | ~$0.001/1K tokens |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | Free tier |
| OpenRouter | `https://openrouter.ai/api/v1` | *(any model slug)* | Varies |
| Ollama (local) | `http://localhost:11434/v1` | `llama3.3` | Free |
| LM Studio (local) | `http://localhost:1234/v1` | *(model loaded in app)* | Free |

---

## What it does under the hood

Claude Code sends requests in Anthropic's Messages API format. Every other provider speaks OpenAI's format. This proxy bridges the two:

- **Translates** `system` blocks, content blocks, tool definitions, and tool results between formats
- **Streams** — converts OpenAI SSE chunks back to Anthropic SSE events in real time
- **Intercepts** housekeeping requests (quota probes, title generation, suggestion mode) and short-circuits them locally so they don't consume provider quota
- **Counts tokens** via tiktoken when Claude Code asks

---

## Telegram bot (optional)

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_ID` in `.env` to control Claude Code sessions remotely from your phone. Send a message to the bot, get the output back.
