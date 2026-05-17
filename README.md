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
cp .env.example .env   # add your provider URL + key
./run.sh               # starts the proxy and opens Claude Code
```

That's it. `run.sh` handles everything — installs deps on first run, starts the proxy, launches Claude Code pointed at it, and shuts everything down cleanly when you're done.

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

## Why this matters

The best coding agent shouldn't be a walled garden.

Claude Code's real value is the **harness** — how it reasons about your codebase, how it chains tools together, how it recovers from errors and tries again. That's the hard part, and Anthropic nailed it.

The model underneath is just an API call. It should be yours to choose.

Backdoor exists because developers deserve to use the best tools without being forced into a single vendor. Run the best agent. Pick the best model for your budget, your privacy requirements, your use case. Keep both.

---

## Open source. No strings.

MIT licensed. Read the code — it's clean, it's simple, it's less than 600 lines. Fork it, change it, build on it.

If a new AI provider launches tomorrow, you can use it with Backdoor the same day. No waiting for a pull request. Just drop in the URL.

---

## Bonus: control Claude Code from your phone

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_ID` in `.env` and you can trigger Claude Code sessions from Telegram. Send a prompt from your phone, get the output back in chat.

---

## How it works under the hood

Claude Code talks to `localhost:8082` thinking it's Anthropic. Backdoor receives the request, translates it from Anthropic's Messages API format to OpenAI's chat completions format, forwards it to your chosen provider, and streams the response back — translated back into Anthropic's SSE format in real time. Tool calls, streaming deltas, token counts — all handled transparently.

A handful of Claude Code's internal housekeeping requests (quota probes, title generation, etc.) are intercepted and short-circuited locally so they don't burn your provider quota.

---

<div align="center">

**Star this if you think the best coding agent should work with any model.**

</div>
