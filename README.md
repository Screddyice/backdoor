# cc-nim-proxy

Translates [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) requests (Anthropic Messages API) into [NVIDIA NIM](https://build.nvidia.com) (OpenAI-compatible) requests, so you can run Claude Code against free NIM models.

## Quick start

```bash
# 1. Install dependencies
uv sync

# 2. Configure
cp .env.example .env
# edit .env — set NVIDIA_NIM_API_KEY and choose a model

# 3. Run the proxy
uv run uvicorn server:app --host 127.0.0.1 --port 8082

# 4. Point Claude Code at the proxy
ANTHROPIC_BASE_URL=http://127.0.0.1:8082 ANTHROPIC_API_KEY=proxy claude
```

## Configuration

See [.env.example](.env.example) for all options. Required:

| Variable | Description |
|---|---|
| `NVIDIA_NIM_API_KEY` | Free at [build.nvidia.com](https://build.nvidia.com) |
| `NVIDIA_NIM_MODEL` | e.g. `meta/llama-3.3-70b-instruct` |

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
NVIDIA NIM API
    │  SSE chunks (OpenAI format)
    ▼
cc-nim-proxy  ──►  translated back to Anthropic SSE
    ▼
Claude Code CLI
```
