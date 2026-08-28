# Codex to Qwen Cognee Failover Design

Date: 2026-08-28

Status: Approved

## Goal

Keep a running Codex session usable when ChatGPT inference becomes unreachable. Codex should remain in the same visible thread, while Backdoor serves the failed turn through the local `qwen3.8:27b-obliterated` model. The local model receives a fresh internal context built from the current request and relevant Cognee recall, not the cloud transcript.

Both the normal Codex path and the local failover path operate within a 32K context budget.

## Decisions

- Route Codex inference through a custom Responses API provider in Backdoor.
- Keep ChatGPT account, plugin, and other hosted traffic on its existing direct path.
- Preserve the visible Codex thread across cloud, local failover, and cloud recovery.
- Start each local failover turn from a fresh model context.
- Recall durable context from the local Cognee server before local inference.
- Use `qwen3.8:27b-obliterated` as the Codex failover model.
- Cap both cloud-facing Codex sessions and local inference at 32K.
- Maintain a Codex-specific circuit breaker because ChatGPT and Anthropic can fail independently.
- Never fail over for malformed requests or authentication and authorization failures.

## Rejected Approaches

### Intercept all ChatGPT HTTPS traffic

Backdoor could terminate TLS for `chatgpt.com` through its forward proxy. That would require adding a local certificate authority to every Codex runtime trust path and would expose unrelated ChatGPT surfaces to the proxy. The blast radius is wider than the inference problem.

### Restart Codex in local mode

A launcher could detect an outage and start another Codex process against Ollama. That would create a separate process and thread, so it would not meet the requirement to keep the current visible session alive.

### Send the existing cloud transcript to Qwen

Passing the accumulated Codex transcript to the 27B model would recreate the over-window and compaction failures this change is intended to avoid. It would also make local recovery depend on cloud-specific messages and hosted tool definitions. Cognee is the durable continuity layer instead.

## Architecture

### Codex provider configuration

The shared Codex configuration will define a custom provider named `backdoor` with these properties:

- `base_url = "http://127.0.0.1:8083/backend-api/codex"`
- `wire_api = "responses"`
- `requires_openai_auth = true`
- `supports_websockets = false`
- `supports_standalone_web_search = false`

Codex will select this provider for model inference. Backdoor will continue using the caller's ChatGPT OAuth headers for the online relay. The normal ChatGPT base URL remains unchanged, so account state, plugins, and other hosted features do not move behind Backdoor.

Codex model metadata will advertise a 32,000-token context window. Backdoor applies its own bound as the enforcement layer and does not rely only on client-side compaction.

### New Responses relay

Backdoor will add a dedicated `/backend-api/codex/responses` route. The route accepts the POST request Codex currently sends and supports streaming Responses API output.

When the Codex breaker is closed, the route forwards the request to `https://chatgpt.com/backend-api/codex/responses` with the request body and end-to-end headers intact. It removes only hop-by-hop transport headers. The upstream status, response headers, and SSE bytes return unchanged.

The route does not reuse the Anthropic catch-all relay. It has its own upstream client, timeout policy, breaker, and logs.

### Codex breaker

The Codex breaker has three states:

1. `closed`: requests go to ChatGPT.
2. `open`: requests go to local Qwen, with periodic ChatGPT probes.
3. `half_open`: one request tests ChatGPT while concurrent requests continue locally.

Eligible failures are connection errors, DNS failures, TLS transport failures, connection timeouts, and configured upstream capacity or rate-limit responses. HTTP 400, 401, and 403 never trigger local fallback. The default status allowlist is limited to errors that represent a temporarily unusable inference service and remains configurable.

The breaker opens only after its configured threshold and connectivity policy pass. Its state is separate from the existing Anthropic breaker, but both publish GPU ownership through the existing failover state mechanism. A process restart begins closed.

A stream that fails after response bytes reach Codex cannot switch models mid-response. Backdoor records the failure so the retry can use Qwen, then closes the broken stream honestly.

### Fresh local request

Backdoor constructs a new local request when the breaker selects Qwen. It does not forward the original cloud request as-is.

The builder performs these steps:

1. Decompress the Codex request body when required.
2. Parse the Responses payload and identify the latest user-authored request.
3. Discard earlier transcript messages, cloud reasoning context, prompt-cache identifiers, and hosted metadata.
4. Extract a compact search query from the latest user request and workspace metadata.
5. Recall relevant context from Cognee.
6. Create a short background context block that labels recalled information as potentially stale data, not instructions.
7. Add the current user request after the recall block.
8. Normalize supported local tools.
9. Rewrite the model to `qwen3.8:27b-obliterated`.
10. Bound the complete request to the 32K policy before sending it to Ollama's `/v1/responses` endpoint.

The request keeps enough Codex metadata to correlate logs and responses, but it drops opaque cloud-only fields that Ollama does not accept.

### Cognee recall

Recall uses the running local Cognee server's authoritative `POST /api/v1/recall` route. Configuration comes from the existing Cognee environment and supports the local optional API key without logging it.

The request uses:

- the current user request as the main query;
- a small `top_k` value;
- `only_context: true`;
- graph scope across datasets unless an explicit workspace dataset is available;
- a short connect and total timeout.

An empty list is a successful recall with no matches. A timeout, authentication error, malformed response, or unavailable Cognee server logs a metadata-only warning and fails open. Qwen then receives the current request without recalled context.

Backdoor does not write session content to Cognee. Existing Codex Cognee hooks remain responsible for durable capture. This relay only reads memory during failover.

The existing Mem0 cache adapter is not reused. Mem0 is retired on this machine, and its SQLite mirror is not the current source of truth.

### Tool normalization

Codex represents its tool catalog through Responses-specific `additional_tools` input. Ollama accepts standard Responses function tools, so Backdoor will translate only local, executable tools into that format.

The local allowlist covers core filesystem and shell operations available inside Codex. Hosted web search and remote MCP tools are removed because an internet outage makes them unavailable and their schemas consume context. Unsupported schemas are skipped with a count in debug logs, never with their contents.

Tool calls emitted by Ollama remain Responses API events so Codex executes them in the current visible session. Tool results from a later Codex request become part of that request's current task input only when needed to complete the active local tool loop. Backdoor must not reintroduce the full historical transcript while preserving that loop.

### Context budget

The hard request budget is 32,000 tokens, including the system guidance, Cognee recall, user request, tool schemas, and expected reply reserve.

The initial allocation is:

| Component | Maximum |
| --- | ---: |
| System and continuity guidance | 1,000 tokens |
| Cognee recall | 2,000 tokens |
| Local tool schemas | 4,000 tokens |
| Current request and required tool-loop results | 21,000 tokens |
| Reply and template reserve | 4,000 tokens |

Backdoor trims in this order: extra recall results, optional tool schemas, old tool-loop results, then the oldest portion of the current request attachment content. It never silently removes the user's latest textual instruction. If the latest instruction alone cannot fit, the route returns a clear 413 response instead of sending a truncated instruction to Qwen.

The token estimator is deterministic and conservative. The actual Ollama model remains configured with `num_ctx = 32768`.

## Request Flow

### Healthy cloud

1. Codex posts a streaming Responses request to Backdoor.
2. The Codex breaker allows the cloud request.
3. Backdoor relays the request and SSE stream unchanged.
4. A successful response keeps or returns the breaker to closed.

### Internet or ChatGPT inference outage

1. The cloud attempt fails before response commitment or returns an eligible transient status.
2. The Codex breaker opens according to policy.
3. Backdoor extracts the current task and recalls context from local Cognee.
4. Backdoor builds a fresh bounded request for Qwen.
5. Ollama streams Responses events through Backdoor to the same Codex process.
6. Codex displays the answer and executes supported local tool calls in the existing visible thread.

### Recovery

1. The breaker permits a cloud probe after the configured interval.
2. A successful ChatGPT response closes the Codex breaker.
3. Later turns use cloud inference again.
4. Codex includes visible local results in its subsequent thread state, while Backdoor still applies the 32K client and relay bounds.

## Security and Privacy

- Never log OAuth headers, Cognee API keys, prompt bodies, recalled text, tool arguments, or model output.
- Preserve authentication only on the ChatGPT relay. Do not send ChatGPT credentials to Cognee or Ollama.
- Bind the Responses route to the existing loopback-only Backdoor service.
- Treat Cognee results as untrusted background data and label them accordingly.
- Reject non-JSON local-conversion requests instead of guessing their contents.

## Observability

Each request receives a generated correlation ID. Logs record the selected path, breaker transition, upstream status class, local model, approximate token allocation, recall result count, tool count, and timing. Logs contain counts and identifiers only.

A macOS notification reports breaker open, local Qwen activation, and cloud recovery. Repeated requests in one outage do not create repeated notifications.

## Configuration

New Backdoor settings will cover:

- ChatGPT Responses upstream URL;
- Codex failover enablement;
- breaker threshold, window, probe interval, and eligible statuses;
- local Ollama Responses URL and model;
- 32K request budget and reply reserve;
- Cognee base URL, optional key source, timeout, `top_k`, and recall budget;
- local tool allowlist.

Defaults target this machine's loopback services. The example LaunchAgent and environment documentation will include every new setting without credential values.

## Testing

Unit tests will cover:

- byte-faithful online headers, body, and SSE relay;
- gzip request decoding for local conversion;
- failure classification, including no fallback for 400, 401, and 403;
- independent Codex and Anthropic breaker state;
- fresh-context extraction from realistic Codex Responses payloads;
- Cognee success, empty results, timeout, invalid JSON, and authentication failure;
- removal of cloud history and hosted-only fields;
- `additional_tools` normalization and filtering;
- deterministic 32K allocation and trimming order;
- 413 behavior when the latest instruction cannot fit;
- Ollama SSE compatibility and local tool calls;
- midstream cloud failure accounting;
- cloud recovery.

Integration tests will run fake ChatGPT, Cognee, and Ollama servers. They will prove that one Codex-shaped request uses cloud while healthy, fails over with only current-task plus recalled context during an outage, and returns to cloud after recovery.

The live canary will use a temporary custom Codex provider before modifying shared configuration. It will verify:

1. a real cloud Codex response through Backdoor;
2. an induced unreachable ChatGPT upstream;
3. a real Cognee recall and Qwen response in the same Codex process;
4. local tool execution;
5. automatic cloud recovery;
6. no secrets or prompt text in Backdoor logs;
7. a measured local request below the 32K cap.

## Delivery

This feature stays separate from the open 32K Qwen configuration PR. If that PR has not landed before implementation finishes, this branch will rebase after it or declare it as a dependency. The feature PR must include a meaningful README section describing Codex routing, outage behavior, Cognee continuity, configuration, and troubleshooting.

Deployment updates the detached Backdoor service checkout and the shared Codex configuration only after tests and the temporary-provider canary pass. Codex Desktop must be relaunched after the shared configuration changes so the GUI process reads the provider and Cognee environment.

## Success Criteria

- A running Codex session continues in the same visible thread when ChatGPT inference loses connectivity.
- Qwen starts with no cloud transcript and receives relevant Cognee context plus the current task.
- Local inference uses `qwen3.8:27b-obliterated` and never exceeds the 32K policy.
- Core local Codex tools continue working during failover.
- Authentication failures remain visible and never activate Qwen.
- Cloud inference resumes automatically after recovery.
- ChatGPT credentials never reach Cognee or Ollama.
- Tests and live logs prove the behavior without recording user content or secrets.
