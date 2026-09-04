# backdoor

## What this is

The hybrid proxy that carries this machine's Claude and Codex traffic. The router
on `:8083` sends `qwen*` model names to local Ollama and passes everything else to
the real Anthropic API; the forward proxy on `:8084` fronts it. Cloud-to-local
failover sits in the request path with no opt-in.

**The breaker opens on one condition: this host being offline.** 429, 529, 401 and
403 are HTTP responses that prove the network works, so they get relayed rather
than failed over.

## Live-control boundary — read before running anything

This repo is the **source**, and the source is where agents work. You may inspect
the live router, edit code here, run tests, and open PRs.

You may **not** touch the live control plane. Shawn operates it himself from an
independent Terminal session with a rescue path open. Machine PreToolUse hooks
enforce this, and they will reject a command or a file edit that merely *names* the
protected artifacts — including this file, which is why the specifics are not
restated here.

**Read the exact boundary in `~/.claude/CLAUDE.md`, section
"Backdoor live-control boundary", before attempting any live operation.**

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

## Known defect

`config.py` maps `qwen-9b` to profile `local-qwen-9b`, which resolves to
`qwen3.5:9b-64k`. **That tag is not pulled on this Mac.** The `local-failover-heavy`
profile points at the same absent tag. Both names resolve, build a route, and then
fail at the provider, which reads as a broken agent rather than a missing model.
Either build the tag or drop both mappings.

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
