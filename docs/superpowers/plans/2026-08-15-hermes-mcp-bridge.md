# Hermes MCP Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Hermes gateway agents over an HTTP MCP surface so any MCP client — including ones that cannot run a local process — can list and control them, converse with them, read their history, and answer their run approvals.

**Architecture:** An MCPServer app over streamable HTTP, co-located with the gateways, fanning out to each profile's own Hermes REST API. A registry file maps profile → port, key env var, and capability tier; tools check the tier before acting. Two auth boundaries: callers present the bridge's key, the bridge presents each gateway's key, and a gateway key never reaches a caller.

**Tech Stack:** Python 3.11+, `mcp` SDK (`mcp.server.mcpserver.MCPServer`), `httpx` (async), `tomllib` (stdlib), pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/specs/hermes-mcp-bridge.md`

## Global Constraints

- **Python `>=3.11`** — matches `pyproject.toml`; `tomllib` is stdlib from 3.11.
- **No deployment identifiers in this repo.** No hostnames, real profile names, port assignments, or keys in code, tests, docs, or commit messages. This repo is public and was scrubbed in `225ae2a`. Real values live in a private operator addendum and reach the bridge only through the registry file and environment.
- **Tests live flat in `tests/`**, named `test_hermes_mcp_*.py`. The repo has no test subdirectories.
- **Run tests as `.venv/bin/python -m pytest`**, never bare `pytest`. A bare `pytest` picks up the first interpreter on PATH, which lacks this project's dependencies and fails during collection in a way that reads like broken tests.
- **`asyncio_mode = "auto"`** is set in `pyproject.toml`. Async test functions need no decorator.
- **`src/hermes_mcp/` must not import from `src/proxy/`.** They are sibling concerns; the proxy never touches MCP.
- **No tool response may contain a gateway `API_SERVER_KEY` value**, in any field, including error text.
- **Never log a key.** Log the env var *name* when a key is missing, never a value or prefix.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/hermes_mcp/__init__.py` | Package marker. Exports nothing. |
| `src/hermes_mcp/registry.py` | Parse and validate the registry file. Profile → port, key env var, tier, unit, home. |
| `src/hermes_mcp/client.py` | One gateway's REST API over async httpx. Converts transport failures into structured state. |
| `src/hermes_mcp/tools.py` | MCP tool definitions and tier enforcement. |
| `src/hermes_mcp/http_server.py` | MCPServer app, bearer auth, boot guard, entry point. |
| `deploy/hermes-mcp-http.service` | systemd unit template. |
| `deploy/registry.example.toml` | Registry shape with placeholder values. |
| `tests/test_hermes_mcp_registry.py` | Registry parsing and validation. |
| `tests/test_hermes_mcp_client.py` | State mapping for every failure mode. |
| `tests/test_hermes_mcp_tools.py` | Read/config tools, fan-out isolation. |
| `tests/test_hermes_mcp_tiers.py` | Tier enforcement, the safety-critical file. |
| `tests/test_hermes_mcp_lifecycle.py` | systemctl and log tools. |
| `tests/test_hermes_mcp_server_auth.py` | Bearer auth and boot guard. |
| `bd` | `cmd_hermes()` + case arm + USAGE line. |
| `qwen` | `QWEN_HERMES=1` branch. |

---

## Task 1: Registry

**Files:**
- Create: `src/hermes_mcp/__init__.py`, `src/hermes_mcp/registry.py`, `deploy/registry.example.toml`
- Modify: `pyproject.toml` (add `mcp>=1.2` dependency, add `src/hermes_mcp` to wheel packages)
- Test: `tests/test_hermes_mcp_registry.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Profile` frozen dataclass with fields `name: str`, `tier: str`, `port: int | None`, `key_env: str | None`, `unit: str | None`, `home: str | None`
  - `TIERS: frozenset[str]` = `{"full", "control_only", "unconfigured"}`
  - `class RegistryError(ValueError)`
  - `load_registry(path: str | Path | None = None) -> dict[str, Profile]`
  - `registry_path() -> Path` — `HERMES_MCP_REGISTRY` env var, else `~/.config/hermes-mcp/registry.toml`

- [ ] **Step 1: Write the failing test**

Create `tests/test_hermes_mcp_registry.py`:

```python
"""The registry is the single source for profile → port, key, and tier.

Every downstream guarantee rests on it. The tier decides whether a profile can
be conversed with, so a typo'd tier must fail loudly at load rather than
silently defaulting to something permissive. Duplicate ports must fail too:
gateways are separate processes on one host, and two profiles claiming a port
means one of them is silently unreachable or, worse, answering for the other.
"""

import pytest

from src.hermes_mcp.registry import (
    Profile,
    RegistryError,
    load_registry,
    registry_path,
)


def _write(tmp_path, body: str):
    p = tmp_path / "registry.toml"
    p.write_text(body, encoding="utf-8")
    return p


FULL = """
[profiles.alpha]
tier = "full"
port = 9001
key_env = "ALPHA_KEY"
unit = "gw-alpha.service"
home = "/srv/gw/alpha"
"""


def test_loads_a_full_profile(tmp_path):
    reg = load_registry(_write(tmp_path, FULL))
    assert set(reg) == {"alpha"}
    alpha = reg["alpha"]
    assert isinstance(alpha, Profile)
    assert (alpha.name, alpha.tier, alpha.port) == ("alpha", "full", 9001)
    assert alpha.key_env == "ALPHA_KEY"
    assert alpha.unit == "gw-alpha.service"


def test_unconfigured_profile_needs_no_port_or_key(tmp_path):
    reg = load_registry(_write(tmp_path, '[profiles.ghost]\ntier = "unconfigured"\n'))
    ghost = reg["ghost"]
    assert ghost.port is None and ghost.key_env is None and ghost.unit is None


def test_unknown_tier_is_rejected(tmp_path):
    body = '[profiles.alpha]\ntier = "fulll"\nport = 9001\nkey_env = "K"\n'
    with pytest.raises(RegistryError) as e:
        load_registry(_write(tmp_path, body))
    assert "fulll" in str(e.value)


def test_full_profile_without_port_is_rejected(tmp_path):
    with pytest.raises(RegistryError) as e:
        load_registry(_write(tmp_path, '[profiles.alpha]\ntier = "full"\nkey_env = "K"\n'))
    assert "port" in str(e.value)


def test_full_profile_without_key_env_is_rejected(tmp_path):
    with pytest.raises(RegistryError) as e:
        load_registry(_write(tmp_path, '[profiles.alpha]\ntier = "full"\nport = 9001\n'))
    assert "key_env" in str(e.value)


def test_duplicate_ports_are_rejected(tmp_path):
    body = (
        '[profiles.alpha]\ntier = "full"\nport = 9001\nkey_env = "A"\n'
        '[profiles.beta]\ntier = "full"\nport = 9001\nkey_env = "B"\n'
    )
    with pytest.raises(RegistryError) as e:
        load_registry(_write(tmp_path, body))
    msg = str(e.value)
    assert "9001" in msg and "alpha" in msg and "beta" in msg


def test_missing_file_is_rejected_with_the_path(tmp_path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(RegistryError) as e:
        load_registry(missing)
    assert str(missing) in str(e.value)


def test_empty_registry_is_rejected(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(_write(tmp_path, "\n"))


def test_registry_path_prefers_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_MCP_REGISTRY", str(tmp_path / "custom.toml"))
    assert registry_path() == tmp_path / "custom.toml"


def test_registry_path_defaults_under_config_home(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_REGISTRY", raising=False)
    assert registry_path().parts[-3:] == (".config", "hermes-mcp", "registry.toml")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.hermes_mcp'`

- [ ] **Step 3: Add the dependency and package**

In `pyproject.toml`, add to `dependencies`:

```toml
    "mcp>=1.2",
```

and change the wheel packages line to:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/proxy", "src/hermes_mcp"]
```

Then: `uv sync`

- [ ] **Step 4: Write the implementation**

Create `src/hermes_mcp/__init__.py` (empty file).

Create `src/hermes_mcp/registry.py`:

```python
"""Profile registry for the Hermes MCP bridge.

A Hermes host runs one gateway process per profile, each with its own
HERMES_HOME, .env and API server port. There is no endpoint fronting them all,
so the bridge needs a map, and this file is the only place that map exists.

Validation is strict on purpose. The tier decides whether a profile may be
conversed with, so an unrecognised tier must fail at load rather than default
to something permissive. Duplicate ports must fail too: two profiles claiming
one port means one is unreachable, or is answering for the other.

Deployment values never live in this repo. The file is read from
HERMES_MCP_REGISTRY, or ~/.config/hermes-mcp/registry.toml.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

TIERS = frozenset({"full", "control_only", "unconfigured"})


class RegistryError(ValueError):
    """The registry is missing, unparseable, or internally inconsistent."""


@dataclass(frozen=True)
class Profile:
    name: str
    tier: str
    port: int | None = None
    key_env: str | None = None
    unit: str | None = None
    home: str | None = None

    @property
    def reachable(self) -> bool:
        """True when the profile has an API server the bridge can call."""
        return self.port is not None and self.key_env is not None


def registry_path() -> Path:
    override = os.environ.get("HERMES_MCP_REGISTRY")
    if override:
        return Path(override)
    return Path.home() / ".config" / "hermes-mcp" / "registry.toml"


def load_registry(path: str | Path | None = None) -> dict[str, Profile]:
    target = Path(path) if path is not None else registry_path()
    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"registry not found at {target}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"registry at {target} is not valid TOML: {exc}") from exc

    entries = raw.get("profiles") or {}
    if not entries:
        raise RegistryError(f"registry at {target} declares no [profiles.*] entries")

    profiles: dict[str, Profile] = {}
    ports: dict[int, str] = {}
    for name, body in entries.items():
        tier = body.get("tier")
        if tier not in TIERS:
            raise RegistryError(
                f"profile {name!r} has tier {tier!r}; expected one of {sorted(TIERS)}"
            )

        port = body.get("port")
        key_env = body.get("key_env")
        if tier in {"full", "control_only"}:
            if port is None:
                raise RegistryError(f"profile {name!r} is {tier} but declares no port")
            if key_env is None:
                raise RegistryError(f"profile {name!r} is {tier} but declares no key_env")

        if port is not None:
            clash = ports.get(port)
            if clash is not None:
                raise RegistryError(
                    f"port {port} is claimed by both {clash!r} and {name!r}; "
                    "concurrently running profiles must not share a port"
                )
            ports[port] = name

        profiles[name] = Profile(
            name=name,
            tier=tier,
            port=port,
            key_env=key_env,
            unit=body.get("unit"),
            home=body.get("home"),
        )

    return profiles
```

Create `deploy/registry.example.toml`:

```toml
# Hermes MCP bridge registry. Copy to ~/.config/hermes-mcp/registry.toml and
# fill in real values. Never commit a filled-in copy: this repo is public.
#
# tier:
#   full          chat + control + history + config
#   control_only  everything except chat. Use for hardened single-purpose
#                 profiles that expose an API server for their own product.
#   unconfigured  listed with a reason; every action refused.
#
# port and key_env are required for full and control_only. key_env names the
# environment variable holding that gateway's API_SERVER_KEY — never the key.
# unit and home are optional; without unit the lifecycle tools refuse, without
# home the log tool refuses.

[profiles.primary]
tier = "full"
port = 9001
key_env = "HERMES_KEY_PRIMARY"
unit = "hermes-gateway.service"
home = "/home/example/.hermes"

[profiles.product-endpoint]
tier = "control_only"
port = 9002
key_env = "HERMES_KEY_PRODUCT"
unit = "hermes-gateway-product.service"
home = "/home/example/.hermes/profiles/product"

[profiles.not-yet-configured]
tier = "unconfigured"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_registry.py -q`
Expected: PASS, 10 tests

- [ ] **Step 6: Add the on-disk wiring guard**

The spec asks for a check against the *real* registry, following the lesson in
`tests/test_bare_route_wiring.py`: every piece can be correct in isolation while
the one file that actually gets loaded is wrong. The registry is deploy-time
config outside this repo, so the test must skip when absent rather than fail CI.

Append to `tests/test_hermes_mcp_registry.py`:

```python
def test_the_real_registry_on_disk_is_loadable_and_consistent():
    """Guard the file that actually gets loaded, not just the parser.

    Skips where no registry is deployed (CI, a fresh clone). Where one exists,
    every guarantee the bridge rests on is asserted against it: load_registry
    itself enforces unique ports and valid tiers, and a full profile must name
    an env var that is actually set, or the bridge 401s that profile at runtime
    with everything looking correctly configured.
    """
    import os

    path = registry_path()
    if not path.exists():
        pytest.skip(f"no registry deployed at {path}")

    reg = load_registry(path)
    assert reg, "a deployed registry declares no profiles"

    for name, profile in reg.items():
        assert profile.tier in {"full", "control_only", "unconfigured"}
        if profile.tier == "unconfigured":
            continue
        assert profile.port, f"{name} is {profile.tier} with no port"
        assert profile.key_env, f"{name} is {profile.tier} with no key_env"
        assert os.environ.get(profile.key_env), (
            f"{name} names key_env {profile.key_env}, which is not set in this "
            "environment; the bridge would 401 that profile at runtime while "
            "the registry looks correct"
        )
```

- [ ] **Step 7: Run the guard**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_registry.py -q`
Expected: PASS, with the new test reported as skipped on any machine without a
deployed registry.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src/hermes_mcp/ deploy/registry.example.toml tests/test_hermes_mcp_registry.py
git commit -m "feat(hermes-mcp): registry with strict tier and port validation"
```

---

## Task 2: Gateway client

**Files:**
- Create: `src/hermes_mcp/client.py`
- Test: `tests/test_hermes_mcp_client.py`

**Interfaces:**
- Consumes: `Profile` from `src.hermes_mcp.registry`.
- Produces:
  - `STATES: frozenset[str]` = `{"ok", "stopped", "unconfigured", "control_only", "unreachable", "unauthorized"}`
  - `state(profile, state, reason=None, next=None) -> dict` — the one structured-state shape every tool returns
  - `class GatewayClient` with `__init__(self, profile: Profile, *, timeout: float = 10.0)`, `async def request(self, method: str, path: str, json: dict | None = None) -> dict`, `async def probe(self) -> dict`
  - `class MissingKey(RuntimeError)`

`request` returns either `{"ok": True, "data": <parsed json>}` or a state dict. It never raises for a transport or auth failure.

- [ ] **Step 1: Write the failing test**

Create `tests/test_hermes_mcp_client.py`:

```python
"""A dead gateway must never take down a listing.

Tools fan out across every profile, so a client that raises on a refused
connection turns one stopped gateway into a total failure of hermes_list. Every
failure mode therefore comes back as a structured state with a reason and a
next action, and the only thing that raises is a programming error.

The key never appears in a response. That is asserted here rather than trusted,
because the natural way to write an auth error message is to include what was
sent.
"""

import httpx
import pytest

from src.hermes_mcp.client import GatewayClient, MissingKey, state
from src.hermes_mcp.registry import Profile

ALPHA = Profile(
    name="alpha", tier="full", port=9001, key_env="ALPHA_KEY",
    unit="gw-alpha.service", home="/srv/gw/alpha",
)
SECRET = "k" * 40


def _client(monkeypatch, handler, profile=ALPHA, key=SECRET):
    if key is None:
        monkeypatch.delenv(profile.key_env, raising=False)
    else:
        monkeypatch.setenv(profile.key_env, key)
    c = GatewayClient(profile)
    monkeypatch.setattr(c, "_transport", httpx.MockTransport(handler))
    return c


def test_state_shape_is_stable():
    s = state("alpha", "stopped", reason="not running", next="start it")
    assert s == {
        "profile": "alpha", "state": "stopped",
        "reason": "not running", "next": "start it",
    }


async def test_successful_request_returns_data(monkeypatch):
    c = _client(monkeypatch, lambda r: httpx.Response(200, json={"models": ["m"]}))
    got = await c.request("GET", "/v1/models")
    assert got == {"ok": True, "data": {"models": ["m"]}}


async def test_bearer_key_is_sent(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    c = _client(monkeypatch, handler)
    await c.request("GET", "/health")
    assert seen["auth"] == f"Bearer {SECRET}"


async def test_connection_refused_is_stopped_not_an_exception(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    c = _client(monkeypatch, handler)
    got = await c.request("GET", "/health")
    assert got["state"] == "stopped"
    assert got["profile"] == "alpha"
    assert got["next"]


async def test_timeout_is_unreachable(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    c = _client(monkeypatch, handler)
    assert (await c.request("GET", "/health"))["state"] == "unreachable"


@pytest.mark.parametrize("code", [401, 403])
async def test_auth_failure_is_unauthorized(monkeypatch, code):
    c = _client(monkeypatch, lambda r: httpx.Response(code, text="nope"))
    got = await c.request("GET", "/health")
    assert got["state"] == "unauthorized"
    assert ALPHA.key_env in got["reason"], "the reason should name the env var to fix"


async def test_no_response_ever_contains_the_key(monkeypatch):
    c = _client(monkeypatch, lambda r: httpx.Response(401, text=f"bad key {SECRET}"))
    got = await c.request("GET", "/health")
    assert SECRET not in repr(got)


async def test_missing_key_raises_before_any_request(monkeypatch):
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json={})

    c = _client(monkeypatch, handler, key=None)
    with pytest.raises(MissingKey) as e:
        await c.request("GET", "/health")
    assert "ALPHA_KEY" in str(e.value)
    assert called["n"] == 0, "a request was sent without a key"


async def test_unreachable_profile_refuses_without_calling(monkeypatch):
    ghost = Profile(name="ghost", tier="unconfigured")
    c = GatewayClient(ghost)
    got = await c.request("GET", "/health")
    assert got["state"] == "unconfigured"


async def test_probe_reports_ok_when_healthy(monkeypatch):
    c = _client(monkeypatch, lambda r: httpx.Response(200, json={"status": "ok"}))
    assert (await c.probe())["state"] == "ok"


async def test_server_error_is_unreachable_with_the_code(monkeypatch):
    c = _client(monkeypatch, lambda r: httpx.Response(503, text="down"))
    got = await c.request("GET", "/health")
    assert got["state"] == "unreachable"
    assert "503" in got["reason"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.hermes_mcp.client'`

- [ ] **Step 3: Write the implementation**

Create `src/hermes_mcp/client.py`:

```python
"""One gateway's REST API, over async httpx.

Tools fan out across every profile, so this layer must never raise for a
gateway that is simply down. Transport and auth failures come back as
structured state with a reason and a next action; only a programming error
(a missing key in the environment) raises.

The gateway key is read from the environment at call time, never stored on the
instance and never echoed into a response.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .registry import Profile

STATES = frozenset(
    {"ok", "stopped", "unconfigured", "control_only", "unreachable", "unauthorized"}
)


class MissingKey(RuntimeError):
    """The env var named by the profile's key_env is unset or empty."""


def state(
    profile: str, state: str, *, reason: str | None = None, next: str | None = None
) -> dict[str, Any]:
    """The one structured-state shape every tool returns for a non-ok profile."""
    assert state in STATES, f"unknown state {state!r}"
    return {"profile": profile, "state": state, "reason": reason, "next": next}


class GatewayClient:
    def __init__(self, profile: Profile, *, timeout: float = 10.0) -> None:
        self.profile = profile
        self.timeout = timeout
        self._transport: httpx.BaseTransport | None = None

    def _key(self) -> str:
        value = os.environ.get(self.profile.key_env or "", "")
        if not value:
            raise MissingKey(
                f"{self.profile.key_env} is unset; the bridge cannot authenticate "
                f"to profile {self.profile.name!r}"
            )
        return value

    async def request(
        self, method: str, path: str, json: dict | None = None
    ) -> dict[str, Any]:
        p = self.profile
        if not p.reachable:
            return state(
                p.name,
                "unconfigured",
                reason="no API server port or key is registered for this profile",
                next="add port and key_env to the registry once the gateway is configured",
            )

        key = self._key()
        url = f"http://127.0.0.1:{p.port}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport
            ) as client:
                resp = await client.request(
                    method, url, json=json,
                    headers={"Authorization": f"Bearer {key}"},
                )
        except httpx.ConnectError:
            return state(
                p.name, "stopped",
                reason=f"nothing is listening on port {p.port}",
                next=(f"start {p.unit}" if p.unit else
                      "no systemd unit is registered; install the gateway first"),
            )
        except httpx.TimeoutException:
            return state(
                p.name, "unreachable",
                reason=f"no response within {self.timeout:g}s",
                next="check whether the gateway is wedged",
            )
        except httpx.HTTPError as exc:
            return state(
                p.name, "unreachable",
                reason=f"transport error: {type(exc).__name__}",
                next="check the gateway process and the registered port",
            )

        if resp.status_code in (401, 403):
            # Deliberately does not include the response body: a gateway is
            # free to echo the key it was sent, and that must not reach a caller.
            return state(
                p.name, "unauthorized",
                reason=f"gateway rejected the key from {p.key_env} ({resp.status_code})",
                next=f"rotate {p.key_env} in the bridge env and the profile .env together",
            )
        if resp.status_code >= 400:
            return state(
                p.name, "unreachable",
                reason=f"gateway returned HTTP {resp.status_code}",
                next="check the gateway logs",
            )

        try:
            data = resp.json()
        except ValueError:
            data = {"text": resp.text}
        return {"ok": True, "data": data}

    async def probe(self) -> dict[str, Any]:
        got = await self.request("GET", "/health")
        if got.get("ok"):
            return state(self.profile.name, "ok")
        return got
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_client.py -q`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add src/hermes_mcp/client.py tests/test_hermes_mcp_client.py
git commit -m "feat(hermes-mcp): gateway client that reports state instead of raising"
```

---

## Task 3: Tier enforcement

This task comes before the tools that use it, because it is the safety-critical
piece. A `control_only` profile is a hardened single-purpose endpoint; offering
it as a chat target is the specific mistake the tier exists to prevent.

**Files:**
- Create: `src/hermes_mcp/tools.py` (tier gate only; tools land in Tasks 4-6)
- Test: `tests/test_hermes_mcp_tiers.py`

**Interfaces:**
- Consumes: `Profile`, `state` from Tasks 1-2.
- Produces:
  - `CHAT_TOOLS: frozenset[str]` — tool names refused for `control_only`
  - `check_tier(profile: Profile, tool: str) -> dict | None` — returns `None` when allowed, a refusal state dict when not
  - `resolve(registry: dict[str, Profile], name: str) -> tuple[Profile | None, dict | None]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_hermes_mcp_tiers.py`:

```python
"""Tier enforcement. The safety-critical file.

A control_only profile is a hardened single-purpose endpoint: narrow SOUL.md,
low iteration cap, dedicated credentials, exposing an API server because its own
product calls it. It is not a general agent. Offering it as a chat target is the
exact mistake the tier exists to prevent, so the refusal is asserted here for
every chat-shaped tool by name, and the refusal has to say why — a generic error
invites a retry.

The list of chat-shaped tools is asserted too. Adding a conversational tool
without adding it to CHAT_TOOLS would silently open control_only profiles to it.
"""

import pytest

from src.hermes_mcp.registry import Profile
from src.hermes_mcp.tools import CHAT_TOOLS, check_tier, resolve

FULL = Profile(name="alpha", tier="full", port=9001, key_env="A", unit="a.service")
LOCKED = Profile(name="prod", tier="control_only", port=9002, key_env="B", unit="b.service")
GHOST = Profile(name="ghost", tier="unconfigured")


@pytest.mark.parametrize("tool", sorted(CHAT_TOOLS))
def test_control_only_refuses_every_chat_tool(tool):
    refusal = check_tier(LOCKED, tool)
    assert refusal is not None, f"{tool} was allowed against a control_only profile"
    assert refusal["state"] == "control_only"
    assert "control_only" in (refusal["reason"] or "")


@pytest.mark.parametrize("tool", sorted(CHAT_TOOLS))
def test_full_allows_every_chat_tool(tool):
    assert check_tier(FULL, tool) is None


@pytest.mark.parametrize("tool", ["hermes_status", "hermes_logs", "hermes_models"])
def test_control_only_allows_non_chat_tools(tool):
    assert check_tier(LOCKED, tool) is None, (
        "control_only means no conversation, not no observability"
    )


@pytest.mark.parametrize("tool", ["hermes_chat", "hermes_status", "hermes_logs"])
def test_unconfigured_refuses_everything(tool):
    refusal = check_tier(GHOST, tool)
    assert refusal is not None
    assert refusal["state"] == "unconfigured"


def test_chat_tools_covers_every_conversational_tool():
    """Pin the membership. A new conversational tool that is not added here
    would be allowed against a control_only profile."""
    assert CHAT_TOOLS == frozenset(
        {"hermes_chat", "hermes_run_status", "hermes_run_stop", "hermes_run_approve"}
    )


def test_resolve_returns_the_profile_when_known():
    reg = {"alpha": FULL}
    profile, err = resolve(reg, "alpha")
    assert profile is FULL and err is None


def test_resolve_reports_an_unknown_profile_with_the_known_names():
    reg = {"alpha": FULL, "prod": LOCKED}
    profile, err = resolve(reg, "nope")
    assert profile is None
    assert "alpha" in err["reason"] and "prod" in err["reason"], (
        "an unknown profile should list what is available"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_tiers.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.hermes_mcp.tools'`

- [ ] **Step 3: Write the implementation**

Create `src/hermes_mcp/tools.py`:

```python
"""MCP tool definitions and the tier gate.

Tools are registered against an MCPServer instance by register_tools(), which
Tasks 4-6 fill in. This file starts with the gate because every tool consults
it first.
"""

from __future__ import annotations

from .client import state
from .registry import Profile

#: Tools that hold a conversation with an agent. Refused for control_only.
#: A new conversational tool MUST be added here or control_only profiles are
#: silently opened to it; tests/test_hermes_mcp_tiers.py pins the membership.
CHAT_TOOLS = frozenset(
    {"hermes_chat", "hermes_run_status", "hermes_run_stop", "hermes_run_approve"}
)


def check_tier(profile: Profile, tool: str) -> dict | None:
    """Return None when *tool* may run against *profile*, else a refusal state."""
    if profile.tier == "unconfigured":
        return state(
            profile.name, "unconfigured",
            reason="this profile has no configuration and cannot be started",
            next="configure the profile, then add port and key_env to the registry",
        )
    if profile.tier == "control_only" and tool in CHAT_TOOLS:
        return state(
            profile.name, "control_only",
            reason=(
                f"{tool} holds a conversation, and this profile is registered "
                "control_only: it is a hardened single-purpose endpoint, not a "
                "general agent"
            ),
            next="use a profile registered as full, or change the tier deliberately",
        )
    return None


def resolve(registry: dict[str, Profile], name: str) -> tuple[Profile | None, dict | None]:
    """Look up *name*, or return a refusal listing the profiles that do exist."""
    profile = registry.get(name)
    if profile is not None:
        return profile, None
    known = ", ".join(sorted(registry)) or "(none)"
    return None, state(
        name, "unconfigured",
        reason=f"no profile named {name!r} in the registry; known profiles: {known}",
        next="check the profile name, or add it to the registry",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_tiers.py -q`
Expected: PASS, 18 tests

- [ ] **Step 5: Commit**

```bash
git add src/hermes_mcp/tools.py tests/test_hermes_mcp_tiers.py
git commit -m "feat(hermes-mcp): tier gate refusing chat against control_only profiles"
```

---

## Task 4: Read and config tools

**Files:**
- Modify: `src/hermes_mcp/tools.py`
- Test: `tests/test_hermes_mcp_tools.py`

**Interfaces:**
- Consumes: `GatewayClient`, `check_tier`, `resolve`.
- Produces: `register_tools(mcp, registry, client_factory=GatewayClient) -> None`, registering `hermes_list`, `hermes_status`, `hermes_sessions`, `hermes_session_messages`, `hermes_models`, `hermes_skills`, `hermes_toolsets`, `hermes_jobs`.

`client_factory` exists so tests inject a fake without patching module globals.

- [ ] **Step 1: Write the failing test**

Create `tests/test_hermes_mcp_tools.py`:

```python
"""Fan-out must be isolated: one dead gateway cannot fail a listing.

hermes_list touches every profile, so the failure that matters is not a bad
response from one gateway but one gateway taking the whole call down with it.
That is asserted directly, with a registry containing a healthy profile, a
refusing profile, and an unconfigured one.
"""

import pytest

from src.hermes_mcp.registry import Profile
from src.hermes_mcp.tools import register_tools

FULL = Profile(name="alpha", tier="full", port=9001, key_env="A", unit="a.service")
LOCKED = Profile(name="prod", tier="control_only", port=9002, key_env="B", unit="b.service")
GHOST = Profile(name="ghost", tier="unconfigured")
REGISTRY = {"alpha": FULL, "prod": LOCKED, "ghost": GHOST}


class FakeMCP:
    """Captures @mcp.tool() registrations so tests can call them directly."""

    def __init__(self):
        self.tools = {}

    def tool(self, *_a, **_k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeClient:
    """A GatewayClient stand-in whose behaviour is chosen per profile."""

    behaviour: dict = {}

    def __init__(self, profile, **_kw):
        self.profile = profile

    async def request(self, method, path, json=None):
        return self.behaviour[self.profile.name]

    async def probe(self):
        return self.behaviour[self.profile.name]


@pytest.fixture
def tools():
    mcp = FakeMCP()
    register_tools(mcp, REGISTRY, client_factory=FakeClient)
    return mcp.tools


async def test_list_returns_every_profile_with_its_tier(tools):
    FakeClient.behaviour = {
        "alpha": {"profile": "alpha", "state": "ok", "reason": None, "next": None},
        "prod": {"profile": "prod", "state": "ok", "reason": None, "next": None},
        "ghost": {"profile": "ghost", "state": "unconfigured", "reason": "x", "next": "y"},
    }
    got = await tools["hermes_list"]()
    by_name = {p["profile"]: p for p in got["profiles"]}
    assert set(by_name) == {"alpha", "prod", "ghost"}
    assert by_name["prod"]["tier"] == "control_only"
    assert by_name["ghost"]["tier"] == "unconfigured"


async def test_one_dead_gateway_does_not_fail_the_listing(tools):
    FakeClient.behaviour = {
        "alpha": {"profile": "alpha", "state": "ok", "reason": None, "next": None},
        "prod": {"profile": "prod", "state": "stopped", "reason": "nothing listening",
                 "next": "start b.service"},
        "ghost": {"profile": "ghost", "state": "unconfigured", "reason": "x", "next": "y"},
    }
    got = await tools["hermes_list"]()
    by_name = {p["profile"]: p for p in got["profiles"]}
    assert by_name["alpha"]["state"] == "ok", "a stopped sibling degraded a healthy profile"
    assert by_name["prod"]["state"] == "stopped"
    assert by_name["prod"]["next"], "a stopped profile should say what to do"


async def test_a_raising_client_is_contained(tools):
    class Exploding(FakeClient):
        async def probe(self):
            if self.profile.name == "prod":
                raise RuntimeError("boom")
            return {"profile": self.profile.name, "state": "ok",
                    "reason": None, "next": None}

    mcp = FakeMCP()
    register_tools(mcp, REGISTRY, client_factory=Exploding)
    got = await mcp.tools["hermes_list"]()
    by_name = {p["profile"]: p for p in got["profiles"]}
    assert by_name["alpha"]["state"] == "ok"
    assert by_name["prod"]["state"] == "unreachable", (
        "an exception inside one probe must not escape hermes_list"
    )


async def test_unknown_profile_is_refused_by_name(tools):
    got = await tools["hermes_status"](profile="nope")
    assert got["state"] == "unconfigured"
    assert "alpha" in got["reason"]


@pytest.mark.parametrize("tool,path", [
    ("hermes_models", "/v1/models"),
    ("hermes_skills", "/v1/skills"),
    ("hermes_toolsets", "/v1/toolsets"),
    ("hermes_jobs", "/api/jobs"),
    ("hermes_sessions", "/api/sessions"),
])
async def test_config_tools_hit_the_documented_endpoint(tool, path):
    seen = {}

    class Recording(FakeClient):
        async def request(self, method, p, json=None):
            seen["path"] = p
            return {"ok": True, "data": {}}

    mcp = FakeMCP()
    register_tools(mcp, REGISTRY, client_factory=Recording)
    await mcp.tools[tool](profile="alpha")
    assert seen["path"] == path


async def test_session_messages_uses_the_session_id():
    seen = {}

    class Recording(FakeClient):
        async def request(self, method, p, json=None):
            seen["path"] = p
            return {"ok": True, "data": {}}

    mcp = FakeMCP()
    register_tools(mcp, REGISTRY, client_factory=Recording)
    await mcp.tools["hermes_session_messages"](profile="alpha", session_id="s-1")
    assert seen["path"] == "/api/sessions/s-1/messages"


async def test_config_tool_against_unconfigured_profile_refuses(tools):
    FakeClient.behaviour = {}
    got = await tools["hermes_models"](profile="ghost")
    assert got["state"] == "unconfigured"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_tools.py -q`
Expected: FAIL — `ImportError: cannot import name 'register_tools'`

- [ ] **Step 3: Write the implementation**

Append to `src/hermes_mcp/tools.py`:

```python
import asyncio
import logging

from .client import GatewayClient

logger = logging.getLogger(__name__)


def register_tools(mcp, registry: dict[str, Profile], *, client_factory=GatewayClient) -> None:
    """Register every bridge tool against *mcp*.

    client_factory is injectable so tests can supply a fake without patching
    module globals.
    """

    async def _probe(profile: Profile) -> dict:
        """Probe one profile. Never raises: hermes_list fans out over these.

        Returns a bare state dict. hermes_list attaches the tier from the
        registry rather than from here, so every branch — including the two
        early returns — carries it.
        """
        try:
            if profile.tier == "unconfigured":
                return state(
                    profile.name, "unconfigured",
                    reason="no configuration; cannot be started",
                    next="configure the profile, then register port and key_env",
                )
            return await client_factory(profile).probe()
        except Exception as exc:
            logger.exception("probe failed for %s", profile.name)
            return state(
                profile.name, "unreachable",
                reason=f"probe raised {type(exc).__name__}",
                next="check the bridge logs",
            )

    async def _call(profile_name: str, tool: str, method: str, path: str,
                    json: dict | None = None) -> dict:
        profile, refusal = resolve(registry, profile_name)
        if refusal is not None:
            return refusal
        refusal = check_tier(profile, tool)
        if refusal is not None:
            return refusal
        return await client_factory(profile).request(method, path, json)

    @mcp.tool()
    async def hermes_list() -> dict:
        """List every registered Hermes profile with its state and tier."""
        names = list(registry)
        results = await asyncio.gather(*(_probe(registry[n]) for n in names))
        return {
            "profiles": [
                {**result, "tier": registry[name].tier}
                for name, result in zip(names, results)
            ]
        }

    @mcp.tool()
    async def hermes_status(profile: str) -> dict:
        """Health and capabilities for one profile."""
        return await _call(profile, "hermes_status", "GET", "/v1/capabilities")

    @mcp.tool()
    async def hermes_sessions(profile: str) -> dict:
        """List conversation sessions on a profile."""
        return await _call(profile, "hermes_sessions", "GET", "/api/sessions")

    @mcp.tool()
    async def hermes_session_messages(profile: str, session_id: str) -> dict:
        """Read the message history of one session."""
        return await _call(
            profile, "hermes_session_messages", "GET",
            f"/api/sessions/{session_id}/messages",
        )

    @mcp.tool()
    async def hermes_models(profile: str) -> dict:
        """Models available to a profile."""
        return await _call(profile, "hermes_models", "GET", "/v1/models")

    @mcp.tool()
    async def hermes_skills(profile: str) -> dict:
        """Skills installed on a profile."""
        return await _call(profile, "hermes_skills", "GET", "/v1/skills")

    @mcp.tool()
    async def hermes_toolsets(profile: str) -> dict:
        """Toolsets enabled on a profile."""
        return await _call(profile, "hermes_toolsets", "GET", "/v1/toolsets")

    @mcp.tool()
    async def hermes_jobs(profile: str) -> dict:
        """Scheduled jobs on a profile."""
        return await _call(profile, "hermes_jobs", "GET", "/api/jobs")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_tools.py tests/test_hermes_mcp_tiers.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_mcp/tools.py tests/test_hermes_mcp_tools.py
git commit -m "feat(hermes-mcp): read and config tools with isolated fan-out"
```

---

## Task 5: Chat, run, and approval tools

**Files:**
- Modify: `src/hermes_mcp/tools.py`
- Test: `tests/test_hermes_mcp_tiers.py` (extend)

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: `hermes_chat`, `hermes_run_status`, `hermes_run_stop`, `hermes_run_approve` registered on `mcp`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_hermes_mcp_tiers.py`:

```python
from src.hermes_mcp.tools import register_tools


class _MCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *_a, **_k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _Client:
    calls = []

    def __init__(self, profile, **_kw):
        self.profile = profile

    async def request(self, method, path, json=None):
        _Client.calls.append((self.profile.name, method, path, json))
        return {"ok": True, "data": {"run_id": "r-1"}}

    async def probe(self):
        return {"profile": self.profile.name, "state": "ok", "reason": None, "next": None}


def _tools():
    mcp = _MCP()
    register_tools(mcp, {"alpha": FULL, "prod": LOCKED, "ghost": GHOST},
                   client_factory=_Client)
    _Client.calls = []
    return mcp.tools


async def test_chat_reaches_a_full_profile():
    t = _tools()
    got = await t["hermes_chat"](profile="alpha", message="hello")
    assert got["ok"] is True
    name, method, path, body = _Client.calls[-1]
    assert (name, method) == ("alpha", "POST")
    assert body["input"] == "hello"


async def test_chat_against_control_only_never_reaches_the_gateway():
    """The refusal must happen before any request. A gateway that answered
    would mean the tier is advisory, which is not what it is for."""
    t = _tools()
    got = await t["hermes_chat"](profile="prod", message="hello")
    assert got["state"] == "control_only"
    assert _Client.calls == [], "a request was sent to a control_only profile"


async def test_run_approve_posts_the_decision():
    t = _tools()
    await t["hermes_run_approve"](profile="alpha", run_id="r-9", approved=True)
    name, method, path, body = _Client.calls[-1]
    assert path == "/v1/runs/r-9/approval"
    assert body == {"approved": True}


async def test_run_stop_posts_to_the_run():
    t = _tools()
    await t["hermes_run_stop"](profile="alpha", run_id="r-9")
    assert _Client.calls[-1][2] == "/v1/runs/r-9/stop"


async def test_run_status_reads_the_run():
    t = _tools()
    await t["hermes_run_status"](profile="alpha", run_id="r-9")
    assert _Client.calls[-1][1:3] == ("GET", "/v1/runs/r-9")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_tiers.py -q`
Expected: FAIL — `KeyError: 'hermes_chat'`

- [ ] **Step 3: Write the implementation**

Append inside `register_tools` in `src/hermes_mcp/tools.py`:

```python
    @mcp.tool()
    async def hermes_chat(profile: str, message: str, session_id: str | None = None) -> dict:
        """Send a prompt to an agent and return its reply.

        Refused for control_only profiles. Hermes applies its own approval
        layer; approvals raised inside the resulting run are answerable with
        hermes_run_approve, but approvals from chat-platform-initiated turns
        still surface on that platform, not here.
        """
        body: dict = {"input": message}
        if session_id:
            body["session_id"] = session_id
        return await _call(profile, "hermes_chat", "POST", "/v1/runs", body)

    @mcp.tool()
    async def hermes_run_status(profile: str, run_id: str) -> dict:
        """Current status of a run started through this bridge."""
        return await _call(profile, "hermes_run_status", "GET", f"/v1/runs/{run_id}")

    @mcp.tool()
    async def hermes_run_stop(profile: str, run_id: str) -> dict:
        """Stop an in-flight run."""
        return await _call(profile, "hermes_run_stop", "POST", f"/v1/runs/{run_id}/stop")

    @mcp.tool()
    async def hermes_run_approve(profile: str, run_id: str, approved: bool) -> dict:
        """Answer an approval request raised inside a run."""
        return await _call(
            profile, "hermes_run_approve", "POST",
            f"/v1/runs/{run_id}/approval", {"approved": approved},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_tiers.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_mcp/tools.py tests/test_hermes_mcp_tiers.py
git commit -m "feat(hermes-mcp): chat, run, and approval tools behind the tier gate"
```

---

## Task 6: Lifecycle and log tools

These do not go through the REST API. They work because the bridge is
co-located with the gateways: `systemctl --user` for lifecycle, and reading each
profile's own `logs/` directory.

**Files:**
- Modify: `src/hermes_mcp/tools.py`
- Test: `tests/test_hermes_mcp_lifecycle.py`

**Interfaces:**
- Consumes: `Profile.unit`, `Profile.home`.
- Produces: `hermes_start`, `hermes_stop`, `hermes_restart`, `hermes_logs` registered on `mcp`; `register_tools` gains a `runner=asyncio.create_subprocess_exec` keyword.

- [ ] **Step 1: Write the failing test**

Create `tests/test_hermes_mcp_lifecycle.py`:

```python
"""Lifecycle and logs bypass REST; they only work because the bridge is
co-located with the gateways.

Two things are asserted that a happy-path test would miss. A profile with no
systemd unit must refuse rather than shell out to `systemctl --user start None`.
And the log path must stay inside the profile's own home, so a crafted
profile name cannot walk out of it.
"""

import pytest

from src.hermes_mcp.registry import Profile
from src.hermes_mcp.tools import register_tools

FULL = Profile(name="alpha", tier="full", port=9001, key_env="A",
               unit="a.service", home="/srv/gw/alpha")
NO_UNIT = Profile(name="nounit", tier="full", port=9003, key_env="C", home="/srv/gw/nounit")
NO_HOME = Profile(name="nohome", tier="full", port=9004, key_env="D", unit="d.service")


class _MCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *_a, **_k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _Proc:
    def __init__(self, rc=0, out=b"", err=b""):
        self.returncode = rc
        self._out, self._err = out, err

    async def communicate(self):
        return self._out, self._err


def _tools(runner):
    mcp = _MCP()
    register_tools(
        mcp,
        {"alpha": FULL, "nounit": NO_UNIT, "nohome": NO_HOME},
        client_factory=lambda p, **k: None,
        runner=runner,
    )
    return mcp.tools


@pytest.mark.parametrize("tool,verb", [
    ("hermes_start", "start"),
    ("hermes_stop", "stop"),
    ("hermes_restart", "restart"),
])
async def test_lifecycle_invokes_systemctl_user(tool, verb):
    seen = {}

    async def runner(*argv, **_kw):
        seen["argv"] = argv
        return _Proc()

    got = await _tools(runner)[tool](profile="alpha")
    assert seen["argv"] == ("systemctl", "--user", verb, "a.service")
    assert got["state"] == "ok"


async def test_lifecycle_refuses_a_profile_with_no_unit():
    called = {"n": 0}

    async def runner(*argv, **_kw):
        called["n"] += 1
        return _Proc()

    got = await _tools(runner)["hermes_start"](profile="nounit")
    assert got["state"] == "stopped"
    assert "unit" in (got["reason"] or "")
    assert called["n"] == 0, "shelled out for a profile with no systemd unit"


async def test_lifecycle_reports_a_failing_systemctl():
    async def runner(*argv, **_kw):
        return _Proc(rc=1, err=b"Failed to start a.service")

    got = await _tools(runner)["hermes_start"](profile="alpha")
    assert got["state"] == "unreachable"
    assert "Failed to start" in (got["reason"] or "")


async def test_logs_read_from_the_profile_home(tmp_path):
    home = tmp_path / "alpha"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "gateway.log").write_text("line1\nline2\nline3\n", encoding="utf-8")

    profile = Profile(name="alpha", tier="full", port=9001, key_env="A",
                      unit="a.service", home=str(home))
    mcp = _MCP()
    register_tools(mcp, {"alpha": profile}, client_factory=lambda p, **k: None,
                   runner=None)
    got = await mcp.tools["hermes_logs"](profile="alpha", lines=2)
    assert got["lines"] == ["line2", "line3"]


async def test_logs_refuse_a_profile_with_no_home():
    got = await _tools(None)["hermes_logs"](profile="nohome")
    assert got["state"] == "unconfigured"
    assert "home" in (got["reason"] or "")


async def test_logs_refuse_a_missing_log_file(tmp_path):
    profile = Profile(name="alpha", tier="full", port=9001, key_env="A",
                      unit="a.service", home=str(tmp_path))
    mcp = _MCP()
    register_tools(mcp, {"alpha": profile}, client_factory=lambda p, **k: None,
                   runner=None)
    got = await mcp.tools["hermes_logs"](profile="alpha")
    assert got["state"] == "unreachable"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_lifecycle.py -q`
Expected: FAIL — `TypeError: register_tools() got an unexpected keyword argument 'runner'`

- [ ] **Step 3: Write the implementation**

Change the `register_tools` signature in `src/hermes_mcp/tools.py` to:

```python
def register_tools(
    mcp,
    registry: dict[str, Profile],
    *,
    client_factory=GatewayClient,
    runner=None,
) -> None:
```

and immediately inside the body, before the inner helpers:

```python
    if runner is None:
        runner = asyncio.create_subprocess_exec
```

Then append inside `register_tools`:

```python
    async def _systemctl(profile_name: str, verb: str) -> dict:
        profile, refusal = resolve(registry, profile_name)
        if refusal is not None:
            return refusal
        refusal = check_tier(profile, f"hermes_{verb}")
        if refusal is not None:
            return refusal
        if not profile.unit:
            return state(
                profile.name, "stopped",
                reason="no systemd unit is registered for this profile",
                next="run `hermes gateway install` for it, then add unit to the registry",
            )
        proc = await runner(
            "systemctl", "--user", verb, profile.unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return state(
                profile.name, "unreachable",
                reason=(err or out).decode("utf-8", "replace").strip()
                or f"systemctl {verb} exited {proc.returncode}",
                next="check `systemctl --user status` for the unit",
            )
        return state(profile.name, "ok", reason=f"systemctl {verb} succeeded")

    @mcp.tool()
    async def hermes_start(profile: str) -> dict:
        """Start a profile's gateway. Requires a registered systemd unit."""
        return await _systemctl(profile, "start")

    @mcp.tool()
    async def hermes_stop(profile: str) -> dict:
        """Stop a profile's gateway."""
        return await _systemctl(profile, "stop")

    @mcp.tool()
    async def hermes_restart(profile: str) -> dict:
        """Restart a profile's gateway.

        This resumes its messaging-platform connections, so side effects that
        were paused while it was down resume too.
        """
        return await _systemctl(profile, "restart")

    @mcp.tool()
    async def hermes_logs(profile: str, lines: int = 50) -> dict:
        """Tail a profile's gateway log."""
        p, refusal = resolve(registry, profile)
        if refusal is not None:
            return refusal
        refusal = check_tier(p, "hermes_logs")
        if refusal is not None:
            return refusal
        if not p.home:
            return state(
                p.name, "unconfigured",
                reason="no home is registered for this profile, so its log path is unknown",
                next="add home to the registry entry",
            )
        path = Path(p.home) / "logs" / "gateway.log"
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return state(
                p.name, "unreachable",
                reason=f"cannot read {path}: {type(exc).__name__}",
                next="check the profile home and whether the gateway has ever started",
            )
        tail = content.splitlines()[-lines:] if lines > 0 else []
        return {"profile": p.name, "state": "ok", "lines": tail}
```

Add to the imports at the top of `tools.py`:

```python
from pathlib import Path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_lifecycle.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/hermes_mcp/tools.py tests/test_hermes_mcp_lifecycle.py
git commit -m "feat(hermes-mcp): lifecycle and log tools via co-located systemctl"
```

---

## Task 7: HTTP server, bearer auth, and boot guard

**Files:**
- Create: `src/hermes_mcp/http_server.py`
- Test: `tests/test_hermes_mcp_server_auth.py`

**Interfaces:**
- Consumes: `load_registry`, `register_tools`.
- Produces:
  - `class KeyGuardError(RuntimeError)`
  - `MIN_KEY_LEN: int = 16`, `PLACEHOLDERS: frozenset[str]`
  - `require_bridge_key() -> str`
  - `build_server(registry=None) -> MCPServer`
  - `main() -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_hermes_mcp_server_auth.py`:

```python
"""The boot guard, mirroring the one Hermes applies to its own API server.

Hermes refuses to start its API server without a key of at least 16 characters,
even on loopback, and this bridge is exposed beyond loopback. Refusing at boot
is the only place the check is reliable: a bridge that starts with a weak key
and rejects requests later is a bridge someone will "fix" by removing the auth.
"""

import pytest

from src.hermes_mcp.http_server import (
    MIN_KEY_LEN,
    KeyGuardError,
    build_server,
    require_bridge_key,
)
from src.hermes_mcp.registry import Profile

REGISTRY = {"alpha": Profile(name="alpha", tier="full", port=9001, key_env="A",
                             unit="a.service", home="/srv/a")}


def test_a_strong_key_is_accepted(monkeypatch):
    monkeypatch.setenv("HERMES_MCP_KEY", "k" * 40)
    assert require_bridge_key() == "k" * 40


def test_a_missing_key_refuses_boot(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_KEY", raising=False)
    with pytest.raises(KeyGuardError) as e:
        require_bridge_key()
    assert "HERMES_MCP_KEY" in str(e.value)


def test_a_short_key_refuses_boot(monkeypatch):
    monkeypatch.setenv("HERMES_MCP_KEY", "k" * (MIN_KEY_LEN - 1))
    with pytest.raises(KeyGuardError) as e:
        require_bridge_key()
    assert str(MIN_KEY_LEN) in str(e.value)


@pytest.mark.parametrize("value", [
    "changeme", "CHANGEME", "your-key-here", "replace_with_key", "xxxxxxxxxxxxxxxxx",
])
def test_placeholder_keys_refuse_boot(monkeypatch, value):
    monkeypatch.setenv("HERMES_MCP_KEY", value)
    with pytest.raises(KeyGuardError):
        require_bridge_key()


def test_the_guard_error_never_contains_the_key(monkeypatch):
    secret = "k" * 5
    monkeypatch.setenv("HERMES_MCP_KEY", secret)
    with pytest.raises(KeyGuardError) as e:
        require_bridge_key()
    assert secret not in str(e.value)


def test_build_server_registers_every_expected_tool(monkeypatch):
    monkeypatch.setenv("HERMES_MCP_KEY", "k" * 40)
    server = build_server(REGISTRY)
    names = {t.name for t in server._tool_manager.list_tools()}
    expected = {
        "hermes_list", "hermes_status", "hermes_sessions", "hermes_session_messages",
        "hermes_models", "hermes_skills", "hermes_toolsets", "hermes_jobs",
        "hermes_chat", "hermes_run_status", "hermes_run_stop", "hermes_run_approve",
        "hermes_start", "hermes_stop", "hermes_restart", "hermes_logs",
    }
    assert expected <= names, f"missing: {sorted(expected - names)}"


def test_build_server_refuses_without_a_key(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_KEY", raising=False)
    with pytest.raises(KeyGuardError):
        build_server(REGISTRY)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_server_auth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.hermes_mcp.http_server'`

- [ ] **Step 3: Write the implementation**

Create `src/hermes_mcp/http_server.py`:

```python
"""MCPServer app exposing Hermes gateways over streamable HTTP.

Two auth boundaries, deliberately distinct. Callers authenticate to the bridge
with HERMES_MCP_KEY. The bridge authenticates to each gateway with that
gateway's own key, named by the registry and read from the environment. A
caller never holds a gateway key.

The boot guard mirrors the one Hermes applies to its own API server, and for a
stronger reason: that one is loopback-only by default, this one is reachable
from outside the host. A bridge that boots with a weak key and rejects requests
later is a bridge someone eventually "fixes" by turning the auth off.
"""

from __future__ import annotations

import logging
import os

from mcp.server.mcpserver import MCPServer

from .registry import Profile, load_registry
from .tools import register_tools

logger = logging.getLogger(__name__)

MIN_KEY_LEN = 16
PLACEHOLDERS = frozenset(
    {"changeme", "change-me", "your-key-here", "replace_with_key",
     "replace-with-key", "secret", "password", "test", "example"}
)


class KeyGuardError(RuntimeError):
    """The bridge key is missing, too short, or a placeholder."""


def require_bridge_key() -> str:
    """Return HERMES_MCP_KEY, or refuse to boot. Never echoes the value."""
    value = os.environ.get("HERMES_MCP_KEY", "")
    if not value:
        raise KeyGuardError("HERMES_MCP_KEY is unset; refusing to start")
    if len(value) < MIN_KEY_LEN:
        raise KeyGuardError(
            f"HERMES_MCP_KEY is shorter than {MIN_KEY_LEN} characters; refusing to start"
        )
    lowered = value.strip().lower()
    if lowered in PLACEHOLDERS or len(set(lowered)) <= 2:
        raise KeyGuardError(
            "HERMES_MCP_KEY looks like a placeholder; refusing to start"
        )
    return value


def build_server(registry: dict[str, Profile] | None = None) -> MCPServer:
    """Build the MCPServer app. Applies the boot guard before registering anything."""
    require_bridge_key()
    if registry is None:
        registry = load_registry()
    logger.info(
        "hermes-mcp: %d profiles registered (%s)",
        len(registry),
        ", ".join(f"{n}:{p.tier}" for n, p in sorted(registry.items())),
    )
    mcp = MCPServer("hermes-mcp")
    register_tools(mcp, registry)
    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_hermes_mcp_server_auth.py -q`
Expected: PASS

Note: `server._tool_manager.list_tools()` is an MCPServer internal. If the installed
`mcp` version exposes tools differently, adjust the assertion to whatever that
version provides and keep the same expected-name set.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. The pre-existing suite is 54 tests; the new files add to that.

- [ ] **Step 6: Commit**

```bash
git add src/hermes_mcp/http_server.py tests/test_hermes_mcp_server_auth.py
git commit -m "feat(hermes-mcp): streamable-HTTP server with a boot-time key guard"
```

---

## Task 8: Operator surface — `bd hermes` and `qwen`

**Files:**
- Modify: `bd` (add `cmd_hermes()`, a `case` arm, a `USAGE` line)
- Modify: `qwen` (add a `QWEN_HERMES=1` branch)

**Interfaces:**
- Consumes: nothing from the Python package. `bd hermes` talks to the bridge over HTTP; `qwen` only writes MCP config.
- Produces: no Python interface.

- [ ] **Step 1: Add the `bd` subcommand**

In `bd`, add above the `case` dispatch:

```bash
cmd_hermes() {
  local sub="${1:-list}"; shift || true
  local url="${HERMES_MCP_URL:-}"
  local key="${HERMES_MCP_KEY:-}"
  if [ -z "$url" ]; then
    echo "HERMES_MCP_URL is not set" >&2; return 1
  fi
  if [ -z "$key" ]; then
    echo "HERMES_MCP_KEY is not set" >&2; return 1
  fi
  case "$sub" in
    list|status|logs)
      curl -sS -X POST "$url" \
        -H "Authorization: Bearer $key" \
        -H 'content-type: application/json' \
        -d "{\"method\":\"tools/call\",\"params\":{\"name\":\"hermes_$sub\",\"arguments\":{$*}}}"
      ;;
    ping)
      curl -sS -o /dev/null -w '%{http_code}\n' "$url" \
        -H "Authorization: Bearer $key"
      ;;
    *) echo "usage: bd hermes {list|status|logs|ping}" >&2; return 1 ;;
  esac
}
```

Add the dispatch arm alongside the others:

```bash
  hermes) shift; cmd_hermes "$@" ;;
```

Add to the `USAGE` heredoc:

```
  hermes {list|status|logs|ping}  Query the Hermes MCP bridge
```

- [ ] **Step 2: Verify the guard rails**

Run: `HERMES_MCP_URL= HERMES_MCP_KEY= ./bd hermes list; echo "exit=$?"`
Expected: `HERMES_MCP_URL is not set` on stderr, `exit=1`

Run: `./bd 2>&1 | grep hermes`
Expected: the new USAGE line appears

- [ ] **Step 3: Add the `qwen` branch**

In `qwen`, alongside the existing `QWEN_MEM0` and `QWEN_MCP` branches, add a branch that runs when `QWEN_HERMES=1`. It writes `~/.cache/backdoor/qwen-hermes-mcp.json` with mode 600, expanding `HERMES_MCP_URL` and `HERMES_MCP_KEY` into the file because Claude Code may not expand `${ENV}` in MCP config, and regenerating it every launch so key rotations carry over:

```bash
if [ "${QWEN_HERMES:-0}" = "1" ]; then
  if [ -z "${HERMES_MCP_URL:-}" ] || [ -z "${HERMES_MCP_KEY:-}" ]; then
    echo "QWEN_HERMES=1 but HERMES_MCP_URL/HERMES_MCP_KEY are unset — MCP stays off" >&2
  else
    mkdir -p "$HOME/.cache/backdoor"
    HERMES_CFG="$HOME/.cache/backdoor/qwen-hermes-mcp.json"
    python3 - "$HERMES_CFG" <<'PY'
import json, os, sys
cfg = {"mcpServers": {"hermes-mcp": {
    "type": "http",
    "url": os.environ["HERMES_MCP_URL"],
    "headers": {"Authorization": "Bearer " + os.environ["HERMES_MCP_KEY"]},
}}}
open(sys.argv[1], "w").write(json.dumps(cfg))
PY
    chmod 600 "$HERMES_CFG"
    MCP_ARGS=(--strict-mcp-config --mcp-config "$HERMES_CFG")
  fi
fi
```

- [ ] **Step 4: Verify it degrades safely**

Run: `QWEN_HERMES=1 HERMES_MCP_URL= HERMES_MCP_KEY= bash -n qwen && echo "syntax ok"`
Expected: `syntax ok`

Run the wrapper's banner path with `QWEN_HERMES=1` and no URL set; expected: the warning prints and MCP stays off rather than the launch failing.

- [ ] **Step 5: Confirm the generated file is not world-readable**

Run: `stat -f '%Lp' ~/.cache/backdoor/qwen-hermes-mcp.json`
Expected: `600`

- [ ] **Step 6: Commit**

```bash
git add bd qwen
git commit -m "feat(hermes-mcp): bd hermes subcommand and QWEN_HERMES opt-in wiring"
```

---

## Task 9: Deploy artifacts and README

**Files:**
- Create: `deploy/hermes-mcp-http.service`
- Modify: `README.md`

**Interfaces:** none.

- [ ] **Step 1: Write the unit template**

Create `deploy/hermes-mcp-http.service`:

```ini
# Hermes MCP bridge. Copy to ~/.config/systemd/user/ and edit the paths.
#
# This unit deliberately does NOT set SuccessExitStatus. The bridge exits 0 on
# SIGTERM. It does not copy Hermes's convention of exiting 1 to provoke
# Restart=on-failure: with Restart=always that buys nothing, and on a
# deliberate stop systemd does not restart at all, records the non-zero exit as
# a failure, and parks the unit in "failed" with NRestarts=0. A gateway unit
# doing exactly that is how a healthy agent can look dead for days after a
# clean shutdown.

[Unit]
Description=Hermes MCP bridge — HTTP MCP surface for Hermes gateways
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/hermes-mcp/http.env
WorkingDirectory=%h/backdoor
ExecStart=%h/backdoor/.venv/bin/python -m src.hermes_mcp.http_server
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=append:%h/logs/hermes-mcp-http.log
StandardError=append:%h/logs/hermes-mcp-http.log

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Update the README**

In `README.md`, change the Design specs table row for the Hermes bridge from
`Approved, not yet implemented` to `Implemented` and add, directly beneath the
table:

```markdown
The bridge ships as `src/hermes_mcp/`, a sibling concern that never touches
`src/proxy/`. It is off by default everywhere: the service is not installed by
this repo, and `qwen` attaches it only when `QWEN_HERMES=1`. Configure it with
a registry file (`deploy/registry.example.toml`) and a `HERMES_MCP_KEY`; the
server refuses to start on a missing, short, or placeholder key. Deployment
identifiers live outside this repo, the way `profiles/*.env` already does.
```

- [ ] **Step 3: Verify the whole suite still passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 4: Verify no deployment identifiers leaked**

The deny-list is itself sensitive, so it does not live in this repo. Keep it in
the private operator addendum as one pattern per line — host and tailnet names,
real profile names, assigned ports, product names — and point the check at it:

```bash
grep -rn -i -f "$HERMES_MCP_DENYLIST" \
  src/hermes_mcp/ deploy/hermes-mcp-http.service deploy/registry.example.toml \
  tests/test_hermes_mcp_*.py docs/specs/hermes-mcp-bridge.md \
  docs/superpowers/plans/2026-08-15-hermes-mcp-bridge.md \
  && { echo "LEAK — the matches above must not be committed here"; exit 1; } \
  || echo "CLEAN"
```

Expected: `CLEAN`

Note the plan file is in the scanned set. An earlier draft of this very step
enumerated the identifiers inline as a grep pattern, which would have committed
the whole deny-list to a public repo — the check being the leak. If you cannot
run it because `HERMES_MCP_DENYLIST` is unset, treat that as a blocker and get
the list, rather than skipping the step.

- [ ] **Step 5: Commit**

```bash
git add deploy/hermes-mcp-http.service README.md
git commit -m "feat(hermes-mcp): systemd unit template and README"
```

---

## Deployment

Deployment happens on the host and is **not** part of this plan's tasks. The
real values live in the private operator addendum. Two preconditions before any
of it:

1. The Hermes checkout on the host must be back on `main`. It is currently on a
   bug-fix branch pending a merge that needs a second approver.
2. Each profile intended as a chat target needs `API_SERVER_ENABLED`,
   `API_SERVER_KEY`, and a non-colliding `API_SERVER_PORT` in its own `.env`,
   and a gateway restart to pick them up. Restarting a running gateway drops
   its chat-platform connections for a few seconds.
