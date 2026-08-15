# Hermes MCP Bridge — design

**Status:** approved, not yet implemented

Deployment specifics (hostnames, profile names, port assignments, keys) are intentionally absent
from this document. They live in a private operator addendum. Everything here is the generic
design.

## Problem

[Hermes Agent](https://github.com/NousResearch/hermes-agent) runs as a messaging gateway. You can
reach it from Telegram, Slack, Discord and friends, and you can control it by shelling into the
host and running `hermes gateway ...`. You cannot reach it from an MCP client such as Claude Code.

This builds that path: list and control gateways, converse with an agent, read its history, and
inspect its configuration, from any MCP surface including ones that cannot run a local process.

## What Hermes already provides

Two facts shape the whole design.

**Hermes ships an HTTP REST API.** It lives at `gateway/platforms/api_server.py`, runs *inside*
the gateway process, and is enabled by environment variables alone. No code changes required:

```
API_SERVER_ENABLED=true
API_SERVER_KEY=<32+ hex>     # startup guard refuses <16 chars or a placeholder, even on loopback
API_SERVER_PORT=<port>       # default 8642
API_SERVER_HOST=127.0.0.1
```

Auth is `Authorization: Bearer <API_SERVER_KEY>`, compared with `hmac.compare_digest`.

| Purpose | Endpoint |
| --- | --- |
| Feature detection | `GET /v1/capabilities` |
| Health | `GET /health`, `GET /health/detailed` |
| Config | `GET /v1/models`, `GET /v1/skills`, `GET /v1/toolsets` |
| Sessions | `GET\|POST /api/sessions`, `GET\|PATCH\|DELETE /api/sessions/{id}` |
| History | `GET /api/sessions/{id}/messages` |
| Converse | `POST /api/sessions/{id}/chat`, `POST /api/sessions/{id}/chat/stream` (SSE) |
| Runs | `POST /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/events` (SSE), `POST /v1/runs/{id}/approval`, `POST /v1/runs/{id}/stop` |
| Jobs | `GET\|POST /api/jobs`, `GET\|PATCH\|DELETE /api/jobs/{id}`, `POST /api/jobs/{id}/{pause,resume,run}` |

Custom headers: `X-Hermes-Session-Id` for continuity, `X-Hermes-Session-Key` for long-term memory
scope.

**Hermes also ships an MCP server, but it is the wrong one for this job.** `mcp_serve.py` (FastMCP)
exposes ten tools: `conversations_list`, `conversation_get`, `messages_read`, `attachments_fetch`,
`events_poll`, `events_wait`, `messages_send`, `channels_list`, `permissions_list_open`,
`permissions_respond`. It is a messaging-channel bridge. It has no tool that runs a Hermes turn,
and it is stdio-only, so it cannot serve a client that has no local process.

The gap is therefore narrow and specific: **an HTTP MCP surface that exposes agent operations.**

## Gateways are per-profile processes

This is the constraint that shapes the tool surface. A Hermes host runs one gateway *process per
profile*, each isolated by its own `HERMES_HOME` with its own `.env`, and therefore its own API
server on its own port. There is no single endpoint that fronts them all.

Consequences the design must handle rather than hide:

- A profile with no `.env` cannot start at all. It is not "stopped", it is unconfigured.
- A profile with no systemd unit cannot be started by the bridge, only by `hermes gateway install`.
- Profiles are not interchangeable. A hardened single-purpose profile (narrow `SOUL.md`, low
  `HERMES_MAX_ITERATIONS`, dedicated credentials) must not be offered as a chat target just
  because it happens to expose an API server.
- Ports must not collide across concurrently running profiles.

## Goals

- Reach Hermes from every MCP surface, including ones that cannot run a local process.
- Cover control, conversation, history, configuration, and run approvals.
- Report each profile's real capability instead of presenting a uniform fleet.
- Leave the Hermes checkout unmodified, so a fork keeps merging cleanly from an active upstream.

## Non-goals

- Configuring profiles that have no `.env`. That is a separate decision with its own authority
  questions.
- Replacing the messaging gateways. Chat platforms remain the primary human interface.
- Reimplementing the ten messaging tools `mcp_serve.py` already provides.

## Architecture

```
MCP client (any surface)
  │  Authorization: Bearer <HERMES_MCP_KEY>
  ▼
https://<gateway-host>/<route>/          reverse proxy / tunnel
  ▼
127.0.0.1:<bridge-port>   hermes-mcp-http   (FastMCP, streamable HTTP)
  │
  ├─ profile A  → 127.0.0.1:<port A>   full
  ├─ profile B  → 127.0.0.1:<port B>   full
  ├─ profile C  → 127.0.0.1:<port C>   control only
  └─ profile D  → none                 unconfigured
```

Two auth boundaries, deliberately distinct. The bridge authenticates its callers with its own
`HERMES_MCP_KEY`. It authenticates to each gateway with that gateway's `API_SERVER_KEY`. A caller
never holds a gateway key, and no tool response ever contains one.

The bridge is **co-located with the gateways**, same host and same user. That is what lets it do
the three things REST cannot: run `systemctl --user` for lifecycle, and read each profile's own
`logs/` directory.

## Components

### `hermes_mcp/` package

| Module | Responsibility |
| --- | --- |
| `registry.py` | Profile → port, key env var, capability tier. Loaded from one config file outside the repo. Fails startup on duplicate ports. |
| `client.py` | Async `httpx` wrapper over one gateway's REST API. Per-profile timeouts. Converts transport failures into structured state, never exceptions. |
| `tools.py` | MCP tool definitions. Fans out across the registry. |
| `http_server.py` | FastMCP app over streamable HTTP. Bearer auth. Refuses to boot on a missing, short, or placeholder key, mirroring Hermes's own guard. |

This repo is otherwise an LLM proxy, and `src/proxy/` never touches MCP. This package is a
sibling concern and does not import from it.

**No identifiers in the repo.** Hostnames, profile names, ports and keys come from deploy-time
config, the same way `profiles/*.env` is already gitignored here.

### Service unit

Modelled on any long-running user service: `EnvironmentFile`, `Restart=always`, `RestartSec=5`,
append-mode logging.

The bridge **exits 0 on SIGTERM**. It deliberately does not copy Hermes's convention of exiting 1
to provoke `Restart=on-failure`. With `Restart=always` that buys nothing, and on a *deliberate*
stop systemd does not restart at all, records the non-zero exit as a failure, and parks the unit
in `failed` with `NRestarts=0`. A gateway unit doing exactly this is how a healthy agent can sit
apparently-dead for days after a clean shutdown. Exiting 0 avoids needing a `SuccessExitStatus`
override at all.

### `bd hermes` subcommand

Follows this repo's established pattern: a `cmd_hermes()` function, a `hermes)` arm in the `case`
dispatch, and a line in the `USAGE` heredoc. Subcommands `list`, `status`, `logs`, `ping`. This is
local operator convenience; it is not the MCP path.

### `qwen` wiring

MCP is off by default in every tier (`MCP_DEFAULT=0`) because the global schema set costs roughly
142K tokens. Hermes joins as an opt-in alongside the existing `QWEN_MEM0` and `QWEN_MCP` branches:
`QWEN_HERMES=1` adds the server to the generated config under `~/.cache/backdoor/`, mode 600,
regenerated each launch so key rotations carry over.

## Tool surface

| Capability | Tools | Backed by |
| --- | --- | --- |
| Control | `hermes_list`, `hermes_status` | `GET /health`, `GET /v1/capabilities`, `systemctl --user show` |
| Control | `hermes_start`, `hermes_stop`, `hermes_restart` | `systemctl --user` |
| Control | `hermes_logs` | each profile's `HERMES_HOME/logs/` |
| Converse | `hermes_chat` | `POST /api/sessions/{id}/chat` |
| Converse | `hermes_run_status`, `hermes_run_stop` | `GET /v1/runs/{id}`, `POST /v1/runs/{id}/stop` |
| Approvals | `hermes_run_approve` | `GET /v1/runs/{id}/events`, `POST /v1/runs/{id}/approval` |
| History | `hermes_sessions`, `hermes_session_messages` | `GET /api/sessions`, `GET /api/sessions/{id}/messages` |
| Config | `hermes_models`, `hermes_skills`, `hermes_toolsets`, `hermes_jobs` | `GET /v1/models`, `/v1/skills`, `/v1/toolsets`, `/api/jobs` |

Every tool takes a `profile` argument except `hermes_list`, which fans out.

**On approvals.** The gating model is to let Hermes's own hook and approval layer decide, rather
than re-confirming in the MCP client. For that to work, approval requests must be answerable in
the session that triggered them. Over REST that is run-scoped: `/v1/runs/{id}/events` streams
`approval.request` and `/v1/runs/{id}/approval` answers it. `hermes_run_approve` wraps the pair.

The broader `permissions_list_open` / `permissions_respond` tools in `mcp_serve.py` cover pending
approvals across messaging channels rather than one run, but that server is stdio-only with no
REST equivalent, so they are not reachable here. An approval raised by a chat-initiated turn still
surfaces on that chat platform. Only approvals inside runs this bridge started are answerable
through it. Closing that gap would require modifying `mcp_serve.py`, which this design avoids.

## Capability tiers

The registry assigns each profile exactly one tier, and tools check it before acting.

| Tier | Meaning |
| --- | --- |
| `full` | chat, control, history, config |
| `control_only` | everything except `hermes_chat` |
| `unconfigured` | listed with the reason; every action refused |

`hermes_chat` against a `control_only` profile returns a refusal naming the tier and the reason,
not a generic error. Silence would invite exactly the mistake the tier exists to prevent.

## Error handling

One dead profile must never fail a listing. Tools return structured state rather than raising:

```json
{"profile": "<name>", "state": "stopped",
 "reason": "no systemd unit; gateway not running",
 "next": "hermes_start is unavailable until `hermes gateway install` creates the unit"}
```

States: `ok`, `stopped`, `unconfigured`, `control_only`, `unreachable`, `unauthorized`.

- Per-profile timeouts, so a hung gateway bounds only its own entry.
- Bearer failures return 401 without echoing key material.
- The bridge never proxies a gateway's `API_SERVER_KEY` to a caller.

## Testing

This repo's convention is to assert on what the provider actually receives, against the real
config on disk, rather than on what a helper returns in isolation. `tests/test_bare_route_wiring.py`
is the model, and its docstring records why: every piece can be correct in isolation while the one
env file that matters is missing a setting, which is how that bug shipped.

Applied here:

1. **Registry wiring.** Against the real registry: every `full` profile resolves a port and a key
   env var; no two enabled profiles share a port; every profile has exactly one tier.
2. **Tier enforcement.** `hermes_chat` against a `control_only` profile refuses, and the refusal
   names the tier.
3. **Isolation.** A fake registry with one unreachable port proves `hermes_list` still returns
   every other profile.
4. **Boot guard.** Missing, short, and placeholder `HERMES_MCP_KEY` each refuse startup.
5. **No key leakage.** No tool response contains any `API_SERVER_KEY` value.

## Deployment outline

Real values live in the private operator addendum.

1. Add `API_SERVER_ENABLED`, `API_SERVER_KEY`, `API_SERVER_PORT` to each profile intended as a
   chat target.
2. `hermes gateway install` for any such profile lacking a systemd unit, then start it.
3. Restart already-running gateways to pick up their API server. Note this interrupts their
   chat-platform connectivity for a few seconds.
4. Install the bridge service with its `EnvironmentFile` at mode 600.
5. Add the reverse-proxy or tunnel route to the bridge port.
6. Register the server with the MCP client.
7. Verify: `hermes_list` returns every profile with the correct tier; `hermes_chat` succeeds
   against a `full` profile and is refused against a `control_only` one.

## Risks

| Risk | Mitigation |
| --- | --- |
| A new authenticated surface is exposed beyond the host | Bearer auth with a boot guard on key strength. The bridge holds gateway keys; callers never see them. |
| A conversational tool is pointed at a hardened single-purpose profile | `control_only` tier, enforced in code and covered by a test. |
| Port collision between concurrently running profiles | Registry is the single source; startup check fails on duplicates. |
| Agents take real-world action from an MCP prompt | Accepted by design. Hermes's own approval layer gates. `hermes_run_approve` surfaces approvals for runs started here; approvals raised by chat-initiated turns still only reach that chat platform. |
| Gateway restarts resume messaging side effects | `hermes_restart` reports which platforms reconnected so the effect is visible. |

## Open items

- Hermes's MCP security scanner can flag legitimate tool descriptions as prompt-injection attempts
  (observed against an unrelated in-house MCP server). If that scanner gates tool availability,
  the same false positive could affect this bridge's descriptions. Check before finalising them.
