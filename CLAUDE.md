# backdoor

## What this is

The hybrid proxy that carries this machine's Claude and Codex traffic. The router
on `:8083` sends `qwen*` model names to local Ollama and passes everything else to
the real Anthropic API; the forward proxy on `:8084` fronts it. Cloud-to-local
failover sits in the request path with no opt-in.

**The breaker opens after a sustained transport outage to Anthropic.** The rest
of the internet may still work; some networks can reach ChatGPT while blocking
Anthropic's edge. HTTP 429, 529, 401 and 403 responses still get relayed because
the provider answered and the caller needs to see that response.

## Live-control boundary — read before running anything

This repo is the **source**, and the source is where agents work. You may inspect
the live router, edit code here, run tests, and open PRs.

You may **not** touch the live control plane. Shawn operates it himself from an
independent Terminal session with a rescue path open. Machine PreToolUse hooks
enforce this, and they will reject a command or a file edit that merely *names* the
protected artifacts — including this file, which is why the specifics are not
restated here.

**That section of `~/.claude/CLAUDE.md` no longer exists.** It was removed on
2026-09-10 along with the router itself, so this file spent that time pointing
agents at safety guidance that was not there — and "go read the rules" failing
silently is worse than having no pointer at all. Until a live router exists again
and the machine rules describe it, treat the boundary as: **inspect freely, change
nothing that is running.** Anything that starts, stops, restarts, deploys to, or
repoints a live router or its launchd job is Shawn's to run, from his own session.

If a tool call comes back refused with a message about the live control plane, that
is this guard doing its job. Do not try to route around it; hand the operation to
Shawn.

## Stack

Python `>=3.11`, managed with **uv** (`uv.lock` committed). pytest is configured in
`pyproject.toml`. No Node toolchain — earlier versions of this file listed
`npm run build` and `npm test`, neither of which exists.

## Commands

```bash
uv sync                       # install
uv run pytest                 # full suite
uv run pytest tests/<file>    # one file
```

When you run a test you **expect** to fail, suppress the test names. A red run exits
0 and gets stored as a success, and test names are declarative sentences that the
memory distiller inverts into rules:

```bash
uv run pytest -q --tb=no tests/<file>::<test> 2>&1 | tail -1
echo "EXPECTED-RED: fails without the fix, as designed"
```

The count proves what the names prove and carries no sentence to invert.

## Layout

| Path | What it holds |
|---|---|
| `src/proxy/` | Router and proxy implementation, including the model-name to profile map in `config.py` |
| `profiles/` | One `.env` per route profile (`PROVIDER_MODEL`, `ROUTE_BARE`, `ROUTE_MAX_INPUT_TOKENS`) |
| `modelfiles/bare/` | Ollama Modelfiles for the bare tags, with the KV sizing notes |
| `tests/` | pytest suite |
| `deploy/`, `local/` | Deployment glue |

## Model tags are custom builds, not pulls

The failover tiers do not exist on any registry. `local-qwen38-obliterated` wants
`qwen3.8:27b-obliterated`, and the 256K fallback wants `qwen3.5:4b-256k`; both are
built locally:

```bash
# 27B failover tier
ollama pull hf.co/OBLITERATUS/Qwen3.8-27B-OBLITERATED:Q4_K_M   # ~17 GB
ollama create qwen3.8:27b-obliterated \
  -f modelfiles/bare/qwen3.8-27b-obliterated.Modelfile

# 256K escalation tier (pick_failover_profile sends oversized sessions here)
ollama pull qwen3.5:4b
modelfiles/build.sh qwen3.5-4b-256k.Modelfile                  # -> qwen3.5:4b-256k
```

The 27B's custom Modelfile is not optional: the source GGUF's template carries no
tool contract, so Ollama answers 400 to any request carrying tools without it.
Build the 256K tag through `modelfiles/build.sh`, not a raw `ollama create` —
the script bakes in the shared system prompt a plain create would omit.

Build **both**. With only the 27B, the escalation path 404s on exactly the long
sessions that needed a wider window.

**Why this matters more than it looks.** Without the tag the breaker opens
correctly, the request routes to Ollama exactly as designed, and Ollama 404s. The
symptom is indistinguishable from "failover is broken", and the router log shows a
clean handoff into a model that is not there. If failover appears not to work,
check `ollama list` before reading any other code.

Build a bare tag from the GGUF tag, never int4/MLX — the MLX engine ignores
`num_ctx` and loads a 262144 window that grows toward 32 GB. Verify with
`ollama ps`, not `ollama show --parameters`.

## Rules that apply here

Machine hard rules: `~/.claude/CLAUDE.md`. Workspace rules: `~/projects/CLAUDE.md`
and `~/projects/AGENTS.md`. Org identity comes from the git `origin` remote.

Durable facts go to **claude-mem**, the only memory on this machine. Search it
before re-deriving a past decision. The `.claude-harness/memory/` tree in this
repo is scaffolding, not a live memory layer.

Every branch gets a PR, and every PR updates this repo's README.
