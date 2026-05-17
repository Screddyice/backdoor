# Contributing to Backdoor

Thanks for wanting to help. Backdoor is intentionally simple — please keep it that way.

## What's worth contributing

- **New provider quirks** — some providers deviate from the OpenAI spec in subtle ways (different streaming formats, missing fields, rate limit headers). If you find one and fix it, open a PR.
- **Bug fixes** — if something breaks, fix it and explain what was wrong.
- **Better optimizations** — Claude Code makes a lot of internal requests. If you find more that can be intercepted and short-circuited without affecting behavior, that's valuable.

## What to avoid

- Adding new dependencies unless absolutely necessary
- Abstractions for hypothetical future providers
- Config options that nobody will use
- Rewriting working code in a different style

## How to run it locally

```bash
git clone https://github.com/ajsai47/backdoor
cd backdoor
cp .env.example .env   # fill in your provider
uv sync
uv run uvicorn server:app --host 127.0.0.1 --port 8082 --reload
```

## Submitting a PR

- Keep it focused — one thing per PR
- Test it with a real Claude Code session, not just a unit test
- Update `.env.example` if you're adding a config option
- No need for a long description — show the before/after behavior

## Adding a new provider

Most providers work out of the box since they all speak OpenAI format. If a provider needs special handling, add it in `src/proxy/client.py` or `src/proxy/translate.py` with a comment explaining why.
