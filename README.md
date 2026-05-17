# cc-nim-proxy

Translates [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) requests (Anthropic Messages API) into any OpenAI-compatible API, so you can run Claude Code against any provider — NVIDIA NIM, DeepSeek, Groq, OpenRouter, Ollama, and more.

## Quick start

```bash
# 1. Install dependencies
uv sync

# 2. Configure
cp .env.example .env
# edit .env — set PROVIDER_BASE_URL, PROVIDER_API_KEY, PROVIDER_MODEL

# 3. Run the proxy
uv run uvicorn server:app --host 127.0.0.1 --port 8082

# 4. Point Claude Code at the proxy
ANTHROPIC_BASE_URL=http://127.0.0.1:8082 ANTHROPIC_API_KEY=proxy claude
```

## Provider examples

| Provider | `PROVIDER_BASE_URL` | `PROVIDER_MODEL` |
|---|---|---|
| NVIDIA NIM (free tier) | `https://integrate.api.nvidia.com/v1` | `meta/llama-3.3-70b-instruct` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| OpenRouter | `https://openrouter.ai/api/v1` | `meta-llama/llama-3.3-70b-instruct` |
| Ollama (local) | `http://localhost:11434/v1` | `llama3.3` |
| LM Studio (local) | `http://localhost:1234/v1` | *(model loaded in LM Studio)* |

## Telegram bot (optional)

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_ID` in `.env`.  
Send any message to the bot to run it as a Claude Code prompt in `CLAUDE_WORKSPACE`.

## How it works

```
Claude Code CLI
    │  POST /v1/messages (Anthropic format)
    ▼
cc-nim-proxy  ──►  fast-path intercepts (quota probes, title gen, etc.)
    │  POST /chat/completions (OpenAI format)
    ▼
Any OpenAI-compatible provider
    │  SSE chunks (OpenAI format)
    ▼
cc-nim-proxy  ──►  translated back to Anthropic SSE
    ▼
Claude Code CLI
```
