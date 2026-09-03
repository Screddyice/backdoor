# Automatic Qwen Context Compaction

**Status:** Draft for review
**Date:** 2026-09-04
**Scope:** Claude Messages and Codex Responses requests that Backdoor routes to local Qwen

## Summary

Backdoor will keep long Claude and Codex tasks usable when their route changes from a cloud
model to local Qwen. Before Backdoor calls Qwen, it will archive the exact client transcript on
the Mac and build a bounded working set that fits the 27B model's 32K context window.

The working set will target 18,000 input tokens and enforce a 22,000-token hard ceiling. It will
preserve the current instruction, unresolved tool activity, recent progress, active file and
error references, and relevant older excerpts. Backdoor will send the full transcript to the
cloud again after the route returns to the cloud model.

The feature covers four entry paths:

- A deliberate Claude `/model qwen` switch
- Claude breaker-driven outage failover
- A deliberate Codex local-Qwen route
- Codex breaker-driven outage failover

The 27B model will remain at its measured 32K daily configuration. A 64K profile may remain an
operator experiment, but automatic compaction will not depend on it.

## Problem

Claude and Codex build and retain conversation history before Backdoor receives a request. A
mature cloud task can contain more than 100K tokens. When its route changes to Qwen, bare-mode
stripping removes large tool catalogs and system material but retains the conversation.

On 2026-09-04, Backdoor received a Claude task with about 165,943 raw tokens and 142,225 tokens
after stripping. It moved the request from the 32K 27B model to the 256K 4B escape tier. The
request fit that tier's advertised window, but prefilling 142K tokens took too long for an
interactive task. The client showed 100% context use and appeared frozen.

The current protections solve narrower cases:

- The `qwen` launcher tells Claude Code about the 32K input window and 4K output reserve.
- Bare mode removes the cloud harness and unused tools.
- External-context handling bounds approved large fetched documents.
- The route ladder sends oversized prompts to wider 4B tiers.
- Codex local rebuilding removes some cloud-only history and allocates a 32K local budget.

None of these protections turns an arbitrary mature task into a small, coherent local working
set across both client protocols.

## Goals

1. Keep the same Claude or Codex task usable after Backdoor selects Qwen.
2. Send no local provider request above its proven input limit.
3. Preserve the exact client transcript locally before selecting a smaller working set.
4. Keep cloud traffic byte-faithful and free from local-window limits.
5. Preserve the current instruction and active tool state across compaction.
6. Retrieve useful older context without a network dependency.
7. Use Cognee for approved durable memory when it is reachable.
8. Coalesce identical retries so several clients do not prefill the same prompt at once.
9. Keep the live router disabled until isolated acceptance tests pass.

## Non-goals

- Invoking Claude Code's `/compact` command from the server
- Changing Claude or Codex context meters
- Sending full private transcripts to Cognee
- Paging a model's KV cache between requests
- Expanding the default 27B context above 32K
- Changing cloud request bodies, responses, or provider selection
- Editing the live service checkout or its launch configuration

## Chosen Approach

Backdoor will use deterministic context virtualization. A shared engine will archive normalized
conversation segments, select a bounded working set, and reconstruct the provider request through
client-specific adapters.

This approach has three advantages over model-generated summarization:

- The first compaction never asks a model to read the oversized prompt that caused the failure.
- Exact transcript segments remain available for later retrieval.
- Tests can prove which content survives and enforce the final token bound.

### Alternatives considered

**Ask the client to compact before switching.** This works when the cloud client remains healthy,
but Backdoor cannot trigger the command and outage failover arrives after that opportunity.

**Increase the 27B context.** The 36GB M5 Max runs the current GGUF 27B at 32K around 17GB
resident. A prior MLX build measured near 19GB at 64K, so 64K is the operational ceiling rather
than a validated setting for the current GGUF. An isolated GGUF canary must prove memory and
latency before that profile can become supported. Larger windows consume the remaining Metal
headroom and make long prefills unresponsive.

**Use Cognee as the transcript store.** Cognee provides durable semantic memory while reachable.
The Mac reaches it through a tunnel to a remote host, so an internet outage can remove memory at
the same moment Backdoor needs failover. Backdoor will keep outage continuity local.

## Architecture

### 1. Client adapters

Two adapters will translate client payloads into a shared segment model:

```text
Claude Messages request ──> Claude adapter ─┐
                                            ├─> context engine ─> bounded segments
Codex Responses request ──> Codex adapter ──┘
```

Each segment will record its client type, role, content type, tool-call identity, canonical JSON,
searchable text, and ordering metadata. The adapters will reconstruct native payloads after the
engine selects segments.

The Claude adapter will preserve paired `tool_use` and `tool_result` blocks. The Codex adapter
will preserve paired function calls and outputs, including calls from the active user turn.
Neither adapter may leave an orphaned call or result in the compacted request.

### 2. Trigger placement

The compaction gate will run after Backdoor chooses a local Qwen route and applies bare-mode and
external-context reduction, but before tier selection and provider invocation:

```text
receive request
  -> try healthy cloud path
  -> choose Qwen route
  -> remove cloud-only prompt material
  -> reduce approved oversized tool results
  -> estimate normalized local input
  -> compact when input exceeds 22K
  -> enforce exact token gate
  -> choose Qwen tier
  -> call provider
```

This placement covers hybrid routing, explicit model routing, profile mode, Claude, and Codex.
Requests at or below the hard ceiling will skip archive selection and retain the current fast
path, except for an optional asynchronous archive write.

### 3. Private transcript store

Backdoor will store transcripts in SQLite under `~/.backdoor/context/transcripts.sqlite3`.
The directory will use mode `0700`; database, WAL, and shared-memory files will use mode `0600`.

The store will use content-addressed segments and ordered lineages. It will match a request to the
lineage with the longest exact message-chain prefix. A divergent request will create a child
lineage. An ambiguous match will create a new lineage rather than mix two tasks.

The archive will contain request-body conversation data only. Backdoor will exclude transport
headers, cookies, environment variables, OAuth material, and provider credentials. A 1GiB soft
cap will retain active lineages and prune inactive lineages older than 30 days.

### 4. Working-set assembly

The engine will select content in this order:

1. Local route system and safety policy
2. Current user instruction, kept in full
3. Unresolved tool or function-call pairs
4. Most recent conversation turns
5. Active paths, symbols, commands, errors, and test results
6. Older segments ranked with lineage-scoped SQLite FTS5 search

The assembler will target 18K input tokens. It will remove low-ranked retrieval, older completed
turns, and verbose historical tool output in that order. It will keep the current instruction in
full and treat call pairs as indivisible units.

The final gate will serialize the Qwen provider prompt and count it with a matching local
tokenizer. The 32K profile will enforce a 22K input ceiling, leaving room for templates and output.
Backdoor will refuse the provider call if it cannot prove that the payload fits.

### 5. Cognee integration

Cognee will remain an optional second memory layer:

- The context engine may inject a bounded recall result when Cognee responds within its timeout.
- Backdoor may store approved public fetched sources under the existing source policy.
- Backdoor will not upload the private transcript archive.
- A Cognee error will not stop local compaction or Qwen generation.

Local FTS5 supplies task continuity during an outage. Cognee supplies durable facts across tasks
while the remote service is reachable.

### 6. Response continuity

Backdoor will return the local Qwen response in the client's native protocol. Claude and Codex
will append that response to their full task histories. Each later Qwen-bound turn will pass
through the compaction gate again.

After Backdoor returns to a cloud route, it will relay the client's full request without applying
the local working set. The cloud model will see the original transcript plus the local responses
that the client recorded.

Backdoor will hash normalized local requests. Identical retries will share one in-flight
generation, and a completed local response will remain reusable for ten minutes. This prevents a
reconnect burst from starting several large prefills.

## Error Handling

Backdoor will use a stateless recent-tail selection if SQLite cannot open or finish within 500ms.
The same token gate will validate that fallback.

If Backdoor cannot build a request under the hard ceiling:

- A deliberate Qwen route will return a client-native 413 with instructions to start a fresh task.
- Breaker-driven failover will return a bounded continuity response so the client does not retry an
  impossible prompt until the cloud route recovers.

Provider timeouts will retain the selected working set and request hash for a retry. Backdoor will
not cache partial streams. Claude and Codex cloud traffic will bypass storage failures and local
compaction errors.

The client may continue to display its estimate of the full task size. Backdoor will report honest
provider usage for the compacted local request and will not falsify context metrics to change the
display.

## Safety Policy

Breaker-driven outage failover will expose inspection tools only: Read, Glob, Grep, and the
internal context search. Deliberate local Qwen routes will retain the tools allowed by their
existing route profile.

Retrieved transcript excerpts will carry an untrusted-history marker. The local system prompt
will tell Qwen to treat them as prior data rather than instructions. FTS queries will remain
lineage-scoped so one task cannot retrieve another task's content.

## Configuration

The feature will ship disabled:

```text
CONTEXT_VIRTUALIZATION=false
CONTEXT_TARGET_INPUT_TOKENS=18000
CONTEXT_HARD_INPUT_TOKENS=22000
CONTEXT_ARCHIVE_MAX_BYTES=1073741824
CONTEXT_ARCHIVE_INACTIVE_DAYS=30
CONTEXT_RESPONSE_CACHE_SECONDS=600
CONTEXT_COGNEE_RECALL=true
```

Claude and Codex adapters will share the token and storage settings. Client-specific switches may
disable one adapter during staged validation, but the accepted end state enables both.

## Testing

### Unit tests

- Exact Claude and Codex adapter round trips
- Lineage matching, divergence, and task isolation
- Paired tool/function-call preservation
- Selection priority and deterministic output
- Exact 22K token enforcement
- Database and WAL permissions
- Archive soft-cap pruning
- Retry coalescing and response-cache expiry
- Cognee timeout and offline behavior

### Integration tests

- A 142K Claude task produces a bounded Qwen request and a valid Claude response.
- A 142K Codex task produces a bounded Qwen request and valid Responses SSE.
- Deliberate model switches and breaker failover use the same engine.
- Healthy Claude and Codex cloud paths remain byte-faithful.
- SQLite, tokenizer, and Cognee failures preserve cloud service.
- An orphaned call pair never reaches Qwen.

### Isolated acceptance canary

The canary will run outside the live router and prove:

- First local text begins within 30 seconds for a warm 27B.
- Local generation finishes within 60 seconds or returns the continuity response.
- `ollama ps` reports a 32,768-token context for the 27B.
- The model remains within the measured memory envelope.
- Claude and Codex retry storms coalesce to one provider request.
- Cloud replay matches the original bytes after local failover ends.

## Rollout

1. Land storage, adapters, selection, and token gates with the feature disabled.
2. Run unit and integration suites in the source checkout.
3. Run the isolated real-Ollama canary for Claude and Codex.
4. Enable shadow mode to record selection sizes without changing provider requests.
5. Review latency, archive growth, and lineage isolation.
6. Ask the operator to enable the feature through the protected live-control workflow.

No agent will edit the detached service checkout, signal the router, or change launchd state.

## Success Criteria

- A mature Claude or Codex task can switch to Qwen without sending more than 22K input tokens.
- The current instruction and active call pairs survive compaction.
- Qwen returns useful text within the local response deadline.
- Cloud traffic remains byte-faithful.
- Cognee and SQLite failures do not interrupt healthy cloud service.
- The 27B remains at 32K and does not exceed its measured memory envelope.
