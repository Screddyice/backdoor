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
