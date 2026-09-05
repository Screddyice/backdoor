# Hermes Delegate-Only Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a static-bearer Hermes bridge tier that exposes one configured profile to Nebby through chat, run status, approval, and stop operations only.

**Architecture:** Extend the existing fail-closed registry and tool gate with `delegate_only`. The new tier requires a live gateway endpoint like `full`, permits exactly the four run-scoped tools already classified in `CHAT_TOOLS`, and refuses every profile-scoped history, configuration, log, and lifecycle tool. A separate deployment can load a one-profile registry and its own bearer without changing the existing OAuth bridge.

**Tech Stack:** Python 3.11+, MCPServer 2.x, pytest, TOML registry.

**Spec:** `docs/superpowers/specs/2026-09-05-slack-personal-agent-delegation-design.md` in the companion `teamnebula-ai/nebos-v2` repository.

## Global Constraints

- `delegate_only` permits only `hermes_chat`, `hermes_run_status`, `hermes_run_approve`, and `hermes_run_stop`.
- `hermes_list` may return a health-safe entry from a one-profile registry; it must not expose session, model, toolset, job, log, configuration, or lifecycle data.
- Unknown tiers fail closed.
- `delegate_only` requires `port` and `key_env`; keys and deployment identifiers stay outside the repository.
- The existing `full`, `control_only`, `unconfigured`, OAuth, and static-bearer behavior stays unchanged.
- This branch does not edit the live `backdoor-service` checkout, launch agents, Caddy, secrets, or running processes.

---

### Task 1: Pin the registry contract

**Files:**
- Modify: `tests/test_hermes_mcp_registry.py`
- Modify: `src/hermes_mcp/registry.py`

**Interfaces:**
- Produces: `TIERS = frozenset({"full", "control_only", "delegate_only", "unconfigured"})`.
- Produces: a reachable `Profile` for `delegate_only` only when both `port` and `key_env` exist.

- [ ] **Step 1: Write failing registry tests**

```py
def test_delegate_only_requires_a_port_and_key(tmp_path):
    with pytest.raises(RegistryError, match="declares no port"):
        load_registry(_write(tmp_path, '[profiles.screddy]\ntier = "delegate_only"\nkey_env = "SCREDDY_KEY"\n'))
    with pytest.raises(RegistryError, match="declares no key_env"):
        load_registry(_write(tmp_path, '[profiles.screddy]\ntier = "delegate_only"\nport = 9003\n'))

def test_delegate_only_loads_as_reachable(tmp_path):
    registry = load_registry(_write(tmp_path, '[profiles.screddy]\ntier = "delegate_only"\nport = 9003\nkey_env = "SCREDDY_KEY"\n'))
    assert registry["screddy"].reachable is True
```

- [ ] **Step 2: Run the registry tests and verify they fail**

Run: `/Users/screddy/projects/SRC/backdoor/.venv/bin/python -m pytest tests/test_hermes_mcp_registry.py -q`

Expected: FAIL because `delegate_only` is not a recognized tier.

- [ ] **Step 3: Extend strict registry validation**

```py
TIERS = frozenset({"full", "control_only", "delegate_only", "unconfigured"})

if tier in {"full", "control_only", "delegate_only"}:
    if port is None:
        raise RegistryError(f"profile {name!r} is {tier} but declares no port")
    if key_env is None:
        raise RegistryError(f"profile {name!r} is {tier} but declares no key_env")
```

- [ ] **Step 4: Run the registry tests**

Run: `/Users/screddy/projects/SRC/backdoor/.venv/bin/python -m pytest tests/test_hermes_mcp_registry.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the verified unit**

```bash
git add src/hermes_mcp/registry.py tests/test_hermes_mcp_registry.py
git commit -m "WIP: validate delegate-only Hermes profiles"
```

### Task 2: Enforce the four-tool ceiling

**Files:**
- Modify: `tests/test_hermes_mcp_tiers.py`
- Modify: `src/hermes_mcp/tools.py`

**Interfaces:**
- Consumes: `CHAT_TOOLS`, whose exact four members are already pinned by tests.
- Produces: `check_tier(profile, tool) -> None` only when a `delegate_only` profile receives a `CHAT_TOOLS` member.

- [ ] **Step 1: Write failing allow and deny tests**

```py
DELEGATE = Profile(name="screddy", tier="delegate_only", port=9003, key_env="SCREDDY_KEY")

@pytest.mark.parametrize("tool", sorted(CHAT_TOOLS))
def test_delegate_only_allows_exactly_run_scoped_tools(tool):
    assert check_tier(DELEGATE, tool) is None

@pytest.mark.parametrize("tool", sorted(NON_CHAT_TOOLS - {"hermes_list"}))
def test_delegate_only_refuses_profile_inventory_and_control(tool):
    refusal = check_tier(DELEGATE, tool)
    assert refusal is not None
    assert refusal["state"] == "delegate_only"
    assert tool in refusal["reason"]
```

- [ ] **Step 2: Run the tier tests and verify they fail**

Run: `/Users/screddy/projects/SRC/backdoor/.venv/bin/python -m pytest tests/test_hermes_mcp_tiers.py -q`

Expected: FAIL because the current unknown-tier branch refuses all four chat tools and does not name the new state.

- [ ] **Step 3: Add the fail-closed gate**

```py
if profile.tier == "delegate_only" and tool not in CHAT_TOOLS:
    return state(
        profile.name,
        "delegate_only",
        reason=f"{tool} is outside the delegate_only tool ceiling",
        next="use chat, run status, run approval, or run stop only",
    )
if profile.tier not in ("full", "control_only", "delegate_only", "unconfigured"):
    return state(...)
```

Keep `hermes_list` as the one tool without a profile argument. A deployment with one profile returns only that profile's health-safe state and tier.

- [ ] **Step 4: Prove every registered profile-scoped tool passes through the gate**

Add an integration assertion that invokes each `NON_CHAT_TOOLS - {"hermes_list"}` member against `DELEGATE` and verifies neither the gateway client nor subprocess runner receives a call.

- [ ] **Step 5: Run tier and server-auth tests**

Run: `/Users/screddy/projects/SRC/backdoor/.venv/bin/python -m pytest tests/test_hermes_mcp_tiers.py tests/test_hermes_mcp_server_auth.py -q`

Expected: PASS, including wrong-bearer 401 tests.

- [ ] **Step 6: Commit the verified unit**

```bash
git add src/hermes_mcp/tools.py tests/test_hermes_mcp_tiers.py
git commit -m "WIP: enforce delegate-only Hermes tool ceiling"
```

### Task 3: Document the isolated deployment shape

**Files:**
- Modify: `deploy/registry.example.toml`
- Modify: `deploy/hermes-mcp-http.service`
- Modify: `README.md`

**Interfaces:**
- Documents: one-profile registry, a dedicated `HERMES_MCP_KEY`, fixed Screddy gateway key reference, and a second service instance.
- Does not contain: real hostnames, ports, profiles, keys, or user-specific filesystem paths.

- [ ] **Step 1: Update the example registry**

```toml
# delegate_only  chat + run status + approval + stop only. Use a separate
#                one-profile registry and bridge bearer for machine delegation.

[profiles.delegate-example]
tier = "delegate_only"
port = 9003
key_env = "HERMES_KEY_DELEGATE"
```

- [ ] **Step 2: Update README and service comments**

Explain that the existing OAuth bridge remains broad and unchanged. The Nebby path runs as a second static-bearer instance with its own environment file, port, service name, one-profile registry, and bearer. State that this PR ships code and examples only; installation, secrets, proxy routing, and restart remain separate operator actions.

- [ ] **Step 3: Run the complete verification matrix**

Run: `/Users/screddy/projects/SRC/backdoor/.venv/bin/python -m pytest -q`

Run: `git diff --check origin/main...HEAD`

Expected: all tests pass and the diff has no whitespace errors.

- [ ] **Step 4: Scan for committed credential values**

Run: `rg -n "HERMES_MCP_KEY=.+|HERMES_KEY_DELEGATE=.+" --glob '!*.example' --glob '!*.md' .`

Expected: no matches containing a value.

- [ ] **Step 5: Commit and push**

```bash
git add README.md deploy/registry.example.toml deploy/hermes-mcp-http.service docs/superpowers/plans/2026-09-05-hermes-delegate-only-tier.md
git commit -m "WIP: document the isolated Hermes delegate bridge"
git push -u origin codex/hermes-delegate-bridge
```

- [ ] **Step 6: Open or update the draft PR**

Create a draft PR against `main`. Link the companion NEBOS PR, list the tests, and state that no live Backdoor control path, Hermes service, Caddy route, or secret changed.

---

## Self-review

- Spec coverage: the plan covers the new tier, exact allowlist, fail-closed denials, wrong-bearer behavior, one-profile registry, separate bridge instance, README, and deployment boundary.
- Placeholder scan: each task includes exact files, test code, implementation code, commands, and outcomes.
- Type consistency: `delegate_only` is the same literal in the registry, tool gate, tests, example, and README. The permitted surface remains the existing four-member `CHAT_TOOLS` set.
