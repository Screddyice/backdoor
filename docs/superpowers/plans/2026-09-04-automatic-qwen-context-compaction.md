# Automatic Qwen Context Virtualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep mature Claude and Codex tasks usable when Backdoor routes them to local Qwen by replacing oversized local prompts with a lineage-scoped 18K–22K working set.

**Architecture:** Client adapters normalize Claude Messages and Codex Responses payloads into ordered context segments. A private SQLite store archives exact segments, a deterministic selector chooses the current instruction, active call pairs, recent turns, and lineage-scoped FTS results, and each adapter reconstructs a native local-provider payload. The cloud paths remain byte-faithful.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLite WAL/FTS5, asyncio, httpx, pytest, local `llama-tokenize`

**Spec:** `docs/superpowers/specs/2026-09-04-automatic-qwen-context-compaction-design.md`

## Global Constraints

- Keep `CONTEXT_VIRTUALIZATION=false` by default.
- Target 18,000 input tokens and enforce 22,000 input tokens for the 32K Qwen profile.
- Keep the current instruction in full and preserve tool/function call pairs as indivisible units.
- Store transcripts under `~/.backdoor/context/` with directory mode `0700` and files mode `0600`.
- Keep the archive under a 1GiB soft cap and prune inactive lineages after 30 days.
- Never upload private transcript segments to any remote memory service.
- Keep claude-mem recall fail-open with its existing timeout.
- Leave healthy Claude and Codex cloud request and response bytes unchanged.
- Cap breaker-driven local output at 1,024 tokens; retain the profile's 4,096-token cap for deliberate Qwen routes.
- Do not change the detached live checkout, launchd files, router process, ports, or dependencies.
- Do not enable the feature in the live environment from this branch.

---

### Task 1: Configuration and route policy

**Files:**

- Modify: `src/proxy/config.py`
- Modify: `src/proxy/bare.py`
- Create: `tests/test_context_config.py`
- Modify: `tests/test_bare.py`

**Interfaces:**

- Produces: `Settings.context_virtualization: bool`
- Produces: `Settings.context_target_input_tokens: int`
- Produces: `Settings.context_hard_input_tokens: int`
- Produces: `Settings.context_archive_path: str`
- Produces: `Settings.context_archive_max_bytes: int`
- Produces: `Settings.context_archive_inactive_days: int`
- Produces: `Settings.context_response_cache_seconds: int`
- Produces: `Settings.context_archive_timeout_seconds: float`
- Produces: `Settings.context_assembly_timeout_seconds: float`
- Produces: `Settings.context_tokenizer_executable: str`
- Produces: `Settings.context_tokenizer_model_path: str`
- Produces: `apply_outage_tool_policy(req: MessagesRequest) -> MessagesRequest`

- [ ] **Step 1: Write failing configuration tests**

```python
def test_context_virtualization_defaults_are_safe():
    settings = Settings(_env_file=None)
    assert settings.context_virtualization is False
    assert settings.context_target_input_tokens == 18_000
    assert settings.context_hard_input_tokens == 22_000
    assert settings.context_archive_max_bytes == 1_073_741_824
    assert settings.context_archive_inactive_days == 30
    assert settings.context_response_cache_seconds == 600
    assert settings.context_archive_timeout_seconds == 0.5
    assert settings.context_assembly_timeout_seconds == 2.5
```

- [ ] **Step 2: Write the failing Claude outage-tool test**

```python
def test_outage_policy_keeps_only_local_inspection_tools():
    request = messages_request_with_tools(
        "Read", "Glob", "Grep", "Bash", "Edit", "WebFetch", "mcp__remote__lookup"
    )
    output = apply_outage_tool_policy(request)
    assert [tool.name for tool in output.tools or []] == ["Read", "Glob", "Grep"]
```

- [ ] **Step 3: Run the focused red tests**

Run: `uv run pytest -q --tb=no tests/test_context_config.py tests/test_bare.py 2>&1 | tail -1`

Expected: a non-zero test count followed by `EXPECTED-RED: context settings and policy are absent`.

- [ ] **Step 4: Add settings and the pure allowlist policy**

```python
READ_ONLY_OUTAGE_TOOLS = ("Read", "Glob", "Grep")

def apply_outage_tool_policy(req: MessagesRequest) -> MessagesRequest:
    output = req.model_copy(deep=True)
    allowed = set(READ_ONLY_OUTAGE_TOOLS)
    output.tools = [tool for tool in (output.tools or []) if tool.name in allowed] or None
    if not output.tools:
        output.tool_choice = None
    return output
```

Set `context_archive_path` to `~/.backdoor/context/transcripts.sqlite3`. Use Pydantic bounds: positive token values, archive size at least 1MiB, positive timeouts, and `context_target_input_tokens <= context_hard_input_tokens` in the existing `@model_validator(mode="after")` method.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_context_config.py tests/test_bare.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/proxy/config.py src/proxy/bare.py tests/test_context_config.py tests/test_bare.py
git commit -m "feat(context): define bounded Qwen route policy"
```

---

### Task 2: Shared segment model and client adapters

**Files:**

- Create: `src/proxy/context_segments.py`
- Create: `src/proxy/claude_context_adapter.py`
- Create: `src/proxy/codex_context_adapter.py`
- Create: `tests/test_context_adapters.py`

**Interfaces:**

- Produces: `ContextSegment(segment_id: str, role: str, kind: str, exact_json: str, searchable_text: str, pair_id: str | None)`
- Produces: `NormalizedContext(client_kind: Literal["claude", "codex"], model: str, segments: tuple[ContextSegment, ...], current_segment_id: str, native: dict[str, Any])`
- Produces: `ContextAdapter` protocol with `normalize(payload: Any) -> NormalizedContext` and `rebuild(context: NormalizedContext, selected_ids: Collection[str], historical_text: str | None = None) -> Any`
- Produces: `ClaudeContextAdapter.normalize(req: MessagesRequest) -> NormalizedContext`
- Produces: `ClaudeContextAdapter.rebuild(context: NormalizedContext, selected_ids: Collection[str]) -> MessagesRequest`
- Produces: `CodexContextAdapter.normalize(payload: dict[str, Any]) -> NormalizedContext`
- Produces: `CodexContextAdapter.rebuild(context: NormalizedContext, selected_ids: Collection[str]) -> dict[str, Any]`

- [ ] **Step 1: Write failing Claude round-trip and tool-pair tests**

```python
def test_claude_adapter_round_trips_exact_messages():
    request = claude_fixture()
    adapter = ClaudeContextAdapter()
    context = adapter.normalize(request)
    rebuilt = adapter.rebuild(context, [segment.segment_id for segment in context.segments])
    assert rebuilt.model_dump(mode="json") == request.model_dump(mode="json")

def test_claude_adapter_assigns_one_pair_id_to_tool_use_and_result():
    context = ClaudeContextAdapter().normalize(claude_fixture())
    paired = [segment for segment in context.segments if segment.pair_id == "toolu_1"]
    assert [segment.kind for segment in paired] == ["tool_use", "tool_result"]
```

- [ ] **Step 2: Write failing Codex round-trip and function-pair tests**

```python
def test_codex_adapter_round_trips_exact_input_items():
    payload = load_codex_fixture()
    adapter = CodexContextAdapter()
    context = adapter.normalize(payload)
    rebuilt = adapter.rebuild(context, [segment.segment_id for segment in context.segments])
    assert rebuilt == payload

def test_codex_adapter_assigns_one_pair_id_to_call_and_output():
    context = CodexContextAdapter().normalize(load_codex_fixture())
    paired = [segment for segment in context.segments if segment.pair_id == "call_local"]
    assert [segment.kind for segment in paired] == ["function_call", "function_call_output"]
```

- [ ] **Step 3: Run the red adapter tests**

Run: `uv run pytest -q --tb=no tests/test_context_adapters.py 2>&1 | tail -1`

Expected: import failure because the adapter modules do not exist.

- [ ] **Step 4: Implement canonical segments and both adapters**

Compute `segment_id` as SHA-256 over canonical JSON containing `client_kind`, role, kind, and exact native content. Extract searchable text from text blocks, tool names, tool arguments, and tool results. Reject payloads without a textual current user instruction. During rebuild, expand every selected `pair_id` to both members before ordering native content by its original ordinal.

```python
@dataclass(frozen=True)
class ContextSegment:
    segment_id: str
    ordinal: int
    role: str
    kind: str
    exact_json: str
    searchable_text: str
    pair_id: str | None = None

@dataclass(frozen=True)
class NormalizedContext:
    client_kind: Literal["claude", "codex"]
    model: str
    segments: tuple[ContextSegment, ...]
    current_segment_id: str
    native: Any

class ContextAdapter(Protocol):
    def normalize(self, payload: Any) -> NormalizedContext: ...
    def rebuild(
        self,
        context: NormalizedContext,
        selected_ids: Collection[str],
        historical_text: str | None = None,
    ) -> Any: ...
```

- [ ] **Step 5: Run adapter tests**

Run: `uv run pytest tests/test_context_adapters.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/proxy/context_segments.py src/proxy/claude_context_adapter.py src/proxy/codex_context_adapter.py tests/test_context_adapters.py
git commit -m "feat(context): normalize Claude and Codex histories"
```

---

### Task 3: Private transcript store and lineage isolation

**Files:**

- Create: `src/proxy/context_store.py`
- Create: `tests/test_context_store.py`

**Interfaces:**

- Consumes: `NormalizedContext`
- Produces: `StoredLineage(lineage_id: str, parent_id: str | None, matched_prefix: int, segment_ids: tuple[str, ...])`
- Produces: `StoredSegment(segment_id: str, ordinal: int, role: str, kind: str, exact_json: str, searchable_text: str, pair_id: str | None)`
- Produces: `ContextStore.archive(context: NormalizedContext) -> StoredLineage`
- Produces: `ContextStore.search(lineage_id: str, query: str, limit: int, exclude_ids: Collection[str]) -> list[StoredSegment]`
- Produces: `ContextStore.get_cached_response(key: str, now: float) -> dict[str, Any] | None`
- Produces: `ContextStore.put_cached_response(key: str, response: dict[str, Any], expires_at: float) -> None`

- [ ] **Step 1: Write failing archive, permission, and exact-content tests**

```python
def test_archive_uses_private_wal_and_exact_segments(tmp_path):
    path = tmp_path / "private" / "transcripts.sqlite3"
    store = ContextStore(path)
    lineage = store.archive(ClaudeContextAdapter().normalize(claude_fixture()))
    stored = store.segments(lineage.lineage_id)
    assert json.loads(stored[-1].exact_json)["role"] == "user"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.journal_mode() == "wal"
```

- [ ] **Step 2: Write failing cross-client lineage tests**

```python
def test_lineages_never_cross_client_kinds(store):
    claude = store.archive(ClaudeContextAdapter().normalize(claude_fixture("shared")))
    codex = store.archive(CodexContextAdapter().normalize(codex_fixture("shared")))
    assert claude.lineage_id != codex.lineage_id
    assert store.search(claude.lineage_id, "codex-only", 6, ()) == []

def test_divergence_creates_child_without_mixing_suffixes(store):
    left = store.archive(ClaudeContextAdapter().normalize(claude_fixture("left-secret")))
    right = store.archive(ClaudeContextAdapter().normalize(claude_fixture("right-secret")))
    assert left.lineage_id != right.lineage_id
    assert right.parent_id == left.lineage_id
```

- [ ] **Step 3: Run the red store tests**

Run: `uv run pytest -q --tb=no tests/test_context_store.py 2>&1 | tail -1`

Expected: import failure because `ContextStore` does not exist.

- [ ] **Step 4: Implement SQLite WAL storage**

Create `lineages`, `segments`, `lineage_segments`, `segments_fts`, and `responses` tables. Partition candidate lineage matching by `client_kind`. Use `BEGIN IMMEDIATE`, a per-instance `threading.RLock`, `busy_timeout=500`, and short-lived connections. Search must join `segments_fts` through `lineage_segments` on the requested lineage ID.

Exclude request headers and process state by accepting `NormalizedContext` only. Secure the directory before database creation and secure the database, WAL, and SHM files after each write.

- [ ] **Step 5: Add response-cache and pruning tests**

```python
def test_cached_response_expires(store):
    store.put_cached_response("key", {"ok": True}, expires_at=20.0)
    assert store.get_cached_response("key", now=19.0) == {"ok": True}
    assert store.get_cached_response("key", now=20.0) is None

def test_prune_keeps_active_lineage_and_removes_old_inactive(store, clock):
    old = archive_at(store, clock, 0, "old")
    active = archive_at(store, clock, 31 * 86_400, "active")
    store.database_size = lambda: store.max_bytes + 1
    assert store.prune_if_needed(now=clock.value) == 1
    assert store.segments(old.lineage_id) == []
    assert store.segments(active.lineage_id)
```

- [ ] **Step 6: Run store tests**

Run: `uv run pytest tests/test_context_store.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/proxy/context_store.py tests/test_context_store.py
git commit -m "feat(context): archive private transcript lineages"
```

---

### Task 4: Deterministic working-set selection and token gate

**Files:**

- Create: `src/proxy/context_window.py`
- Create: `src/proxy/context_tokenizer.py`
- Create: `tests/test_context_window.py`
- Create: `tests/test_context_tokenizer.py`

**Interfaces:**

- Produces: `SelectionResult(selected_ids: tuple[str, ...], retrieved: tuple[StoredSegment, ...], estimated_tokens: int, reason: str | None)`
- Produces: `select_working_set(context: NormalizedContext, store: ContextStore, lineage: StoredLineage, target_tokens: int, hard_tokens: int, count: Callable[[Collection[str], Collection[StoredSegment]], int]) -> SelectionResult`
- Produces: `TokenCount(tokens: int, exact: bool, method: str)`
- Produces: `QwenTokenGate.count(payload: dict[str, Any]) -> TokenCount`
- Produces: `QwenTokenGate.require_fit(payload: dict[str, Any], hard_tokens: int) -> TokenCount`

- [ ] **Step 1: Write failing priority and pair-integrity tests**

```python
@pytest.mark.parametrize("adapter", [ClaudeContextAdapter(), CodexContextAdapter()])
def test_selector_keeps_current_instruction_and_active_pair(adapter, store):
    context = adapter.normalize(oversized_fixture())
    lineage = store.archive(context)
    result = select_working_set(context, store, lineage, 18_000, 22_000, fake_counter)
    assert context.current_segment_id in result.selected_ids
    selected_pairs = {s.pair_id for s in context.segments if s.segment_id in result.selected_ids and s.pair_id}
    for pair_id in selected_pairs:
        assert sum(s.pair_id == pair_id and s.segment_id in result.selected_ids for s in context.segments) == 2
```

- [ ] **Step 2: Write failing lineage-scoped retrieval and hard-limit tests**

```python
def test_selector_retrieves_only_from_current_lineage(store):
    wanted, other = archive_two_lineages(store)
    result = select_working_set(wanted.context, store, wanted.lineage, 18_000, 22_000, fake_counter)
    text = " ".join(segment.searchable_text for segment in result.retrieved)
    assert "wanted-marker" in text
    assert "other-marker" not in text

def test_token_gate_rejects_payload_above_hard_limit(fake_tokenizer):
    gate = QwenTokenGate(executable=fake_tokenizer, model_path="model.gguf")
    with pytest.raises(ContextLimitError):
        gate.require_fit({"messages": [{"content": "large"}]}, hard_tokens=22_000)
```

- [ ] **Step 3: Run the red selector tests**

Run: `uv run pytest -q --tb=no tests/test_context_window.py tests/test_context_tokenizer.py 2>&1 | tail -1`

Expected: import failures for the selector and token gate.

- [ ] **Step 4: Implement selection order and historical markers**

Keep current instruction, active pair, recent turns, active paths/errors, and FTS results in that order. Wrap retrieved material in `<backdoor-historical-context>` and state that it is untrusted prior conversation. Remove lowest-ranked retrieved excerpts first, followed by oldest completed turns. Return `reason="current_instruction_over_limit"` or `reason="active_pair_over_limit"` when mandatory content cannot fit.

- [ ] **Step 5: Implement exact tokenizer with conservative fallback**

Render the provider JSON in the same message/tool order used by the Ollama client. Invoke `llama-tokenize` with the configured local GGUF and a 12-second timeout. If the executable or model is unavailable, use the full UTF-8 byte length as a conservative token upper bound. Raise `ContextLimitError` unless one method proves the payload fits.

- [ ] **Step 6: Run selector and tokenizer tests**

Run: `uv run pytest tests/test_context_window.py tests/test_context_tokenizer.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/proxy/context_window.py src/proxy/context_tokenizer.py tests/test_context_window.py tests/test_context_tokenizer.py
git commit -m "feat(context): build bounded Qwen working sets"
```

---

### Task 5: Runtime, archive queue, and retry coalescing

**Files:**

- Create: `src/proxy/context_runtime.py`
- Create: `tests/test_context_runtime.py`

**Interfaces:**

- Produces: `ContextRuntime.archive_cloud(context: NormalizedContext) -> None`
- Produces: `ContextRuntime.prepare_local(context: NormalizedContext, adapter: ContextAdapter, settings: Settings) -> PreparedContext`
- Produces: `PreparedContext(payload: Any, request_hash: str, lineage_id: str | None, token_count: int, used_store: bool)`
- Produces: `ContextRuntime.run_complete_once(key: str, factory: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]`
- Produces: `ContextRuntime.stream_once(key: str, factory: Callable[[], AsyncIterator[str]]) -> AsyncIterator[str]`
- Produces: `ContextRuntime.close() -> None`
- Produces: `normalized_request_hash(client_kind: str, payload: Any) -> str`

- [ ] **Step 1: Write failing archive queue and storage-fallback tests**

```python
async def test_cloud_archive_never_blocks_when_queue_is_full(runtime, context):
    runtime._archive_queue.put_nowait(context)
    runtime.archive_cloud(context)
    assert runtime.archive_dropped == 1

async def test_prepare_local_uses_recent_tail_when_store_fails(runtime, adapter, context):
    runtime.store.archive = Mock(side_effect=ContextStoreUnavailable())
    prepared = await runtime.prepare_local(context, adapter, Settings(context_virtualization=True))
    assert prepared.token_count <= 22_000
    assert prepared.used_store is False
```

- [ ] **Step 2: Write failing complete and streaming coalescing tests**

```python
async def test_identical_complete_retries_share_one_factory(runtime):
    factory = AsyncMock(return_value={"id": "msg_local"})
    results = await asyncio.gather(
        runtime.run_complete_once("same", factory),
        runtime.run_complete_once("same", factory),
    )
    assert results == [{"id": "msg_local"}, {"id": "msg_local"}]
    factory.assert_awaited_once()

async def test_identical_stream_retries_share_one_generation(runtime):
    calls = 0
    async def factory():
        nonlocal calls
        calls += 1
        for event in ("one", "two"):
            yield event
    left, right = await asyncio.gather(collect(runtime.stream_once("same", factory)), collect(runtime.stream_once("same", factory)))
    assert left == right == ["one", "two"]
    assert calls == 1
```

- [ ] **Step 3: Run the red runtime tests**

Run: `uv run pytest -q --tb=no tests/test_context_runtime.py 2>&1 | tail -1`

Expected: import failure because `ContextRuntime` does not exist.

- [ ] **Step 4: Implement bounded archive and preparation operations**

Use `asyncio.to_thread` for SQLite and tokenization. Apply `context_archive_timeout_seconds` and `context_assembly_timeout_seconds` through `asyncio.timeout`. Build a stateless newest-first selection on timeout or store failure, then run the same hard token gate.

- [ ] **Step 5: Implement retry coalescing and ten-minute completed cache**

Use one task per normalized request hash for non-stream responses. For streams, keep a replay buffer and one queue per subscriber. Cache complete streams only after the terminal event. Remove failed and cancelled entries so a later retry can start one new generation.

- [ ] **Step 6: Run runtime tests**

Run: `uv run pytest tests/test_context_runtime.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/proxy/context_runtime.py tests/test_context_runtime.py
git commit -m "feat(context): coalesce compacted local requests"
```

---

### Task 6: Claude Messages route integration

**Files:**

- Modify: `src/proxy/routes.py`
- Create: `tests/test_context_claude_routes.py`
- Modify: `tests/test_claude_cloud_context.py`
- Modify: `tests/test_bare_failover_wiring.py`

**Interfaces:**

- Consumes: `ClaudeContextAdapter`, `ContextRuntime`, `Settings`
- Produces: `_prepare_claude_local(req: MessagesRequest, settings: Settings, failed_over: bool) -> PreparedContext`
- Preserves: `_try_upstream` byte-faithful cloud return path

- [ ] **Step 1: Write a failing 142K deliberate-switch route test**

```python
async def test_deliberate_qwen_switch_compacts_142k_claude_history(app, provider):
    payload = claude_payload_with_history(tokens=142_000, model="qwen")
    response = await app.post("/v1/messages", json=payload)
    assert response.status_code == 200
    sent = provider.last_request
    assert estimate_tokens(sent) <= 22_000
    assert "current-user-marker" in sent.model_dump_json()
```

- [ ] **Step 2: Write failing breaker and cloud-bypass tests**

```python
async def test_claude_breaker_uses_same_compactor(app, open_breaker, provider):
    response = await app.post("/v1/messages", json=claude_payload_with_history(142_000, "claude-opus-5"))
    assert response.status_code == 200
    assert estimate_tokens(provider.last_request) <= 22_000
    assert {tool.name for tool in provider.last_request.tools or []} <= {"Read", "Glob", "Grep"}

async def test_healthy_claude_cloud_body_remains_byte_identical(app, cloud):
    body = raw_oversized_claude_body()
    await app.post("/v1/messages", content=body, headers={"content-type": "application/json"})
    assert cloud.last_body == body
```

- [ ] **Step 3: Run the red Claude route tests**

Run: `uv run pytest -q --tb=no tests/test_context_claude_routes.py tests/test_claude_cloud_context.py 2>&1 | tail -1`

Expected: the deliberate route still escalates the full transcript instead of sending at most 22K.

- [ ] **Step 4: Integrate after route selection and before tier escalation**

Normalize the stripped request only after Backdoor knows the provider is local Qwen. If the estimate exceeds 22K and virtualization is enabled, call `ContextRuntime.prepare_local`. Apply the outage allowlist only when `failed_over=True`. Pass the compacted estimate into tier selection so the 27B remains selected when the working set fits.

Queue cloud archive writes only after upstream accepts the request. Do not parse or archive before `_try_upstream` returns a cloud response.

- [ ] **Step 5: Add deliberate 413 and outage-continuity tests**

```python
async def test_deliberate_qwen_returns_413_when_current_instruction_cannot_fit(app):
    response = await app.post("/v1/messages", json=single_instruction(tokens=23_000, model="qwen"))
    assert response.status_code == 413

async def test_outage_returns_continuity_message_when_compaction_cannot_fit(app, open_breaker):
    response = await app.post("/v1/messages", json=single_instruction(tokens=23_000, model="claude-opus-5"))
    assert response.status_code == 200
    assert "local inference could not fit this turn" in response.text
```

- [ ] **Step 6: Run Claude route and existing route suites**

Run: `uv run pytest tests/test_context_claude_routes.py tests/test_claude_cloud_context.py tests/test_bare_failover_wiring.py tests/test_routes.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/proxy/routes.py tests/test_context_claude_routes.py tests/test_claude_cloud_context.py tests/test_bare_failover_wiring.py
git commit -m "feat(context): compact Claude tasks on Qwen routes"
```

---

### Task 7: Codex Responses route integration

**Files:**

- Modify: `src/proxy/codex_context.py`
- Modify: `src/proxy/codex_routes.py`
- Create: `tests/test_context_codex_routes.py`
- Modify: `tests/test_codex_context.py`
- Modify: `tests/test_codex_routes.py`

**Interfaces:**

- Consumes: `CodexContextAdapter`, `ContextRuntime`, `PreparedContext`
- Produces: `build_virtualized_local_payload(payload: dict[str, Any], prepared: PreparedContext, memories: Sequence[str], settings: Settings) -> tuple[dict[str, Any], CodexBudget]`
- Preserves: `/backend-api/codex/responses/compact` cloud relay behavior

- [ ] **Step 1: Write a failing 142K Codex local-route test**

```python
async def test_codex_qwen_route_compacts_142k_history(app, ollama, open_codex_breaker):
    payload = codex_payload_with_history(tokens=142_000)
    response = await app.post("/backend-api/codex/responses", json=payload)
    assert response.status_code == 200
    assert estimate_codex_tokens(ollama.last_payload) <= 22_000
    assert "current-codex-marker" in json.dumps(ollama.last_payload)
```

- [ ] **Step 2: Write failing Codex pair and SSE tests**

```python
async def test_codex_compaction_keeps_active_function_pair(app, ollama):
    await app.post("/backend-api/codex/responses", json=codex_payload_with_active_call())
    items = ollama.last_payload["input"]
    assert [item["type"] for item in items if item.get("call_id") == "call_active"] == ["function_call", "function_call_output"]

async def test_codex_compacted_stream_keeps_valid_responses_sse(app, ollama):
    ollama.stream(valid_local_responses_events())
    response = await app.post("/backend-api/codex/responses", json=codex_payload_with_history(142_000))
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "response.completed" in response.text
```

- [ ] **Step 3: Run the red Codex tests**

Run: `uv run pytest -q --tb=no tests/test_context_codex_routes.py tests/test_codex_context.py 2>&1 | tail -1`

Expected: current active-turn rebuilding omits archived older task state and cannot use the shared selector.

- [ ] **Step 4: Extend the current Codex builder**

Keep existing tool normalization, reasoning sanitization, encoded-body bounds, and 4K reply reserve. Replace active-turn-only input selection with the adapter's selected native items. Add bounded claude-mem recall after deterministic local selection, then drop recalled memories first if the final exact token gate exceeds 22K.

Route both an already-open breaker and the failure that opens it through the shared runtime. Keep `codex_compact` as a direct cloud relay; Backdoor's local virtualization must not rewrite that endpoint.

- [ ] **Step 5: Add Codex cloud-fidelity and failure tests**

```python
async def test_healthy_codex_cloud_body_remains_byte_identical(app, cloud):
    body = gzip.compress(raw_codex_body())
    await app.post("/backend-api/codex/responses", content=body, headers={"content-encoding": "gzip"})
    assert cloud.last_body == body

async def test_codex_local_returns_413_when_current_instruction_exceeds_22k(app, open_codex_breaker):
    response = await app.post("/backend-api/codex/responses", json=codex_single_instruction(23_000))
    assert response.status_code == 413
```

- [ ] **Step 6: Run Codex suites**

Run: `uv run pytest tests/test_context_codex_routes.py tests/test_codex_context.py tests/test_codex_routes.py tests/test_codex_config.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/proxy/codex_context.py src/proxy/codex_routes.py tests/test_context_codex_routes.py tests/test_codex_context.py tests/test_codex_routes.py
git commit -m "feat(context): compact Codex tasks on Qwen routes"
```

---

### Task 8: Internal historical retrieval and local-memory boundary

**Files:**

- Modify: `src/proxy/context_runtime.py`
- Modify: `src/proxy/context_window.py`
- Modify: `src/proxy/external_context.py`
- Create: `tests/test_context_search_round.py`
- Modify: `tests/test_external_context.py`

**Interfaces:**

- Produces: `INTERNAL_SEARCH_TOOL_NAME = "backdoor_context_search"`
- Produces: `ContextRuntime.internal_rounds(request_hash: str) -> int`
- Produces: `ContextRuntime.build_search_followup(prepared: PreparedContext, request: InternalSearchRequest, max_segments: int, max_tokens: int) -> Any`
- Produces: `parse_internal_search(events: Sequence[dict[str, Any]]) -> InternalSearchRequest | None`
- Produces: `build_search_followup(prepared: PreparedContext, results: Sequence[StoredSegment], adapter: ContextAdapter) -> Any`

- [ ] **Step 1: Write failing internal-search tests for both clients**

```python
@pytest.mark.parametrize("adapter", [ClaudeContextAdapter(), CodexContextAdapter()])
def test_internal_search_runs_once_and_caps_results(adapter, runtime):
    prepared = prepared_fixture(adapter)
    request = InternalSearchRequest(query="old migration failure", call_id="internal_1")
    followup = runtime.build_search_followup(prepared, request, max_segments=6, max_tokens=2_000)
    assert runtime.token_gate.count(followup).tokens <= prepared.token_count + 2_000
    assert runtime.internal_rounds(prepared.request_hash) == 1
```

- [ ] **Step 2: Write failing memory privacy and outage tests**

```python
async def test_private_transcript_segments_are_never_sent_to_remote_memory(runtime, memory_worker):
    await runtime.prepare_local(private_context(), ClaudeContextAdapter(), settings())
    assert memory_worker.remembered == []

async def test_memory_failure_keeps_local_selection_working(runtime, memory_worker):
    memory_worker.recall.side_effect = httpx.ConnectError("offline")
    prepared = await runtime.prepare_local(oversized_context(), ClaudeContextAdapter(), settings())
    assert prepared.token_count <= 22_000
```

- [ ] **Step 3: Run the red search tests**

Run: `uv run pytest -q --tb=no tests/test_context_search_round.py tests/test_external_context.py 2>&1 | tail -1`

Expected: the internal search interfaces do not exist.

- [ ] **Step 4: Implement one bounded internal search round**

Expose the internal tool only to the local provider payload. Intercept its call before returning client events, query the current lineage, inject at most six segments and 2,000 tokens, and allow one second provider call. Refuse a second internal call for the same request hash. Disable the round after 20 seconds of elapsed local service time.

- [ ] **Step 5: Preserve the current external-source policy**

Keep `prepare_external_context` and `prepare_codex_external_context` limited to approved public fetched sources. Do not call `remember_document` for transcript segments. Inject claude-mem recall after local selection and remove it before selected transcript content when enforcing the hard limit.

- [ ] **Step 6: Run search and external-context tests**

Run: `uv run pytest tests/test_context_search_round.py tests/test_external_context.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/proxy/context_runtime.py src/proxy/context_window.py src/proxy/external_context.py tests/test_context_search_round.py tests/test_external_context.py
git commit -m "feat(context): retrieve bounded local history"
```

---

### Task 9: Verification, documentation, and isolated canary

**Files:**

- Create: `scripts/context-compaction-canary.py`
- Modify: `README.md`
- Modify: `profiles/README-local.md`
- Modify: `tests/test_context_candidate.py`
- Modify: `tests/test_qwen38_obliterated_wiring.py`

**Interfaces:**

- Consumes: both adapters, shared runtime, source-checkout router fixture
- Produces: one non-live canary with Claude, Codex, offline, retry, and memory checks

- [ ] **Step 1: Add static rollout-boundary tests**

```python
def test_context_virtualization_remains_disabled_by_default():
    assert Settings(_env_file=None).context_virtualization is False

def test_readme_names_both_clients_and_live_control_boundary():
    readme = Path("README.md").read_text()
    assert "Claude Messages" in readme
    assert "Codex Responses" in readme
    assert "CONTEXT_VIRTUALIZATION=false" in readme
```

- [ ] **Step 2: Build the isolated canary**

The script must start an in-process or temporary-port source router and use temporary archive files. It must not call service-manager commands or write under the detached service checkout. Generate synthetic 142K Claude and Codex histories, assert local provider inputs at or below 22K, run two identical concurrent requests, disable local memory, corrupt the temporary SQLite path, and replay one healthy cloud request byte-for-byte.

Print one line per result:

```text
PASS claude-142k-bounded
PASS codex-142k-bounded
PASS retry-coalesced
PASS memory-offline
PASS sqlite-fallback
PASS cloud-byte-faithful
PASS live-boundary-untouched
```

- [ ] **Step 3: Document behavior and operator boundary**

Document the 18K target, 22K hard limit, private archive location, 1GiB/30-day retention, deliberate-versus-breaker tool policy, local-memory boundary, and the fact that the client context gauge may still reflect full cloud history. State that 32K remains the supported 27B profile and 64K requires a separate isolated GGUF acceptance test.

- [ ] **Step 4: Run focused context suites**

Run: `uv run pytest tests/test_context_*.py tests/test_external_context.py tests/test_bare.py tests/test_bare_failover_wiring.py tests/test_codex_context.py tests/test_codex_routes.py tests/test_claude_cloud_context.py -q`

Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`

Expected: PASS with only documented skips.

- [ ] **Step 6: Run the isolated canary**

Run: `uv run python scripts/context-compaction-canary.py`

Expected: all seven `PASS` lines and exit code 0.

- [ ] **Step 7: Audit the diff and protected boundary**

Run: `git diff --check origin/main...HEAD && git status --short`

Expected: no whitespace errors, no untracked implementation files, and no changes under live service or LaunchAgent paths.

- [ ] **Step 8: Commit**

```bash
git add README.md profiles/README-local.md scripts/context-compaction-canary.py tests/test_context_candidate.py tests/test_qwen38_obliterated_wiring.py
git commit -m "test(context): verify automatic Qwen compaction"
```

---

## Completion Gate

- [ ] Every task commit is pushed to the existing draft PR.
- [ ] The README describes both Claude and Codex behavior.
- [ ] Focused and full pytest suites pass.
- [ ] The isolated canary passes without touching the live service.
- [ ] The feature remains disabled by default.
- [ ] PR review confirms cloud byte fidelity and transcript isolation.
- [ ] Live activation remains an operator-owned follow-up.
