```
██████╗  █████╗  ██████╗██╗  ██╗██████╗  ██████╗  ██████╗ ██████╗
██╔══██╗██╔══██╗██╔════╝██║ ██╔╝██╔══██╗██╔═══██╗██╔═══██╗██╔══██╗
██████╔╝███████║██║     █████╔╝ ██║  ██║██║   ██║██║   ██║██████╔╝
██╔══██╗██╔══██║██║     ██╔═██╗ ██║  ██║██║   ██║██║   ██║██╔══██╗
██████╔╝██║  ██║╚██████╗██║  ██╗██████╔╝╚██████╔╝╚██████╔╝██║  ██║
╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚═════╝  ╚═════╝  ╚═════╝ ╚═╝  ╚═╝
```

**Use Claude Code with any AI — not just Anthropic.**

---

## What is this?

Claude Code is one of the best AI coding tools out there. The problem is it only works with Anthropic's API, which costs money and requires an account.

Backdoor fixes that. It sits between Claude Code and the internet, quietly swapping out the Anthropic API for whatever AI provider you want. DeepSeek, Groq, Ollama running on your own machine — anything. Claude Code never knows the difference. You get the same experience, your way.

---

## Why would you use this?

**You want to try Claude Code without paying.**
Several providers have generous free tiers — NVIDIA NIM and Groq both let you make thousands of requests per month for free. Backdoor lets you use Claude Code on top of those.

**You want to spend less.**
DeepSeek costs about 95% less than Claude's API for the same amount of work. If you're using Claude Code heavily, that adds up fast.

**You want to run everything locally.**
Point Backdoor at Ollama or LM Studio and nothing ever leaves your machine. No API keys, no internet, no usage bills — just your computer doing the work.

**You want to try different models.**
Swap one line in a config file and Claude Code is running on a completely different AI. Great for figuring out which model works best for your specific projects.

**You want to use OpenRouter.**
OpenRouter gives you access to 200+ models through a single API key. Backdoor works with it out of the box, so you can switch between models whenever you want.

---

## Getting started

You need [uv](https://docs.astral.sh/uv/) installed. That's it.

```bash
# 1. Clone the repo
git clone https://github.com/ajsai47/backdoor
cd backdoor

# 2. Set up your provider
cp .env.example .env
# open .env and fill in your provider URL, API key, and model name

# 3. Start it up
./run.sh
```

`run.sh` starts Backdoor and opens Claude Code automatically. When you quit Claude Code, Backdoor shuts down too.

---

## Picking a provider

Open `.env` and set these three values:

```
PROVIDER_BASE_URL=...
PROVIDER_API_KEY=...
PROVIDER_MODEL=...
```

Here's what to put in for the most popular options:

| Provider | BASE_URL | MODEL | Cost |
|---|---|---|---|
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | `meta/llama-3.3-70b-instruct` | Free tier available |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | Very cheap |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | Free tier available |
| OpenRouter | `https://openrouter.ai/api/v1` | any model slug | Varies |
| Ollama (local) | `http://localhost:11434/v1` | any model you've pulled | Free |
| LM Studio (local) | `http://localhost:1234/v1` | whichever model is loaded | Free |

---

## Open source and built to be flexible

Backdoor works with any provider that speaks the OpenAI API format — which is most of them. If a new provider launches tomorrow, you can use it the same day by dropping in their URL and key. No waiting for updates.

The whole thing is open source. Read the code, change it, make it your own. There's no lock-in here — not to a provider, not to us.

---

## Optional: control it from Telegram

If you add a `TELEGRAM_BOT_TOKEN` and your Telegram user ID to `.env`, you can send prompts to Claude Code from your phone and get the responses back in chat.

---

## How it works (the short version)

Claude Code sends requests in Anthropic's format. Every other provider uses a different format. Backdoor translates between the two in real time — including streaming responses, tool use, and everything else Claude Code relies on. It also short-circuits a handful of internal housekeeping requests that Claude Code makes automatically, so they don't eat into your provider quota.
