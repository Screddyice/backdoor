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

from src.hermes_mcp.client import REDACTED, GatewayClient, MissingKey, state
from src.hermes_mcp.registry import Profile
from src.hermes_mcp.tools import register_tools

ALPHA = Profile(
    name="alpha", tier="full", port=9001, key_env="ALPHA_KEY",
    unit="gw-alpha.service", home="/srv/gw/alpha",
)
SECRET = "k" * 40

#: A gateway key holding a non-ASCII character. httpx encodes header values as
#: ASCII, so building "Bearer <key>" from this raises UnicodeEncodeError, whose
#: message names the offending character and its offset. Composed only of "é"
#: and digits so a test can assert that neither any character of the key nor any
#: offset reaches the caller: no digit belongs in a clean response here.
NON_ASCII_KEY = "é" + "7" * 39


def _client(monkeypatch, handler, profile=ALPHA, key=SECRET):
    if key is None:
        monkeypatch.delenv(profile.key_env, raising=False)
    else:
        monkeypatch.setenv(profile.key_env, key)
    c = GatewayClient(profile)
    monkeypatch.setattr(c, "_transport", httpx.MockTransport(handler))
    return c


class _MCP:
    """Captures @mcp.tool() registrations so a test can call them directly."""

    def __init__(self):
        self.tools = {}

    def tool(self, *_a, **_k):
        def deco(fn):
            self.tools[_k.get("name", fn.__name__)] = fn
            return fn
        return deco


def _tool_surface(monkeypatch, handler, key=SECRET):
    """register_tools() wired to real GatewayClients over a MockTransport.

    The leak claim is about what a *caller* receives, and a caller receives a
    tool result, not a client return value. Asserting only at the client layer
    would miss anything the tool layer adds, so the same cases run through both.
    """
    monkeypatch.setenv(ALPHA.key_env, key)
    transport = httpx.MockTransport(handler)

    def factory(profile, **kw):
        c = GatewayClient(profile, **kw)
        c._transport = transport
        return c

    mcp = _MCP()
    register_tools(mcp, {"alpha": ALPHA}, client_factory=factory)
    return mcp.tools


def _raiser(build):
    def handler(request):
        raise build(request)
    return handler


#: Every shape of gateway behaviour that could carry the key back out: success
#: bodies (JSON, nested JSON, a JSON list, and the non-JSON text fallback), the
#: auth codes, another 4xx, a 5xx, and each failure path — connect, timeout, a
#: generic httpx.HTTPError, and an exception from outside httpx's hierarchy.
#: Each handler echoes the key the way a chatty gateway or a raising library
#: would. Keyed by name so a failure names the case.
_LEAK_CASES = {
    "200-json": lambda r: httpx.Response(200, json={"echo": f"Bearer {SECRET}"}),
    "200-json-nested": lambda r: httpx.Response(
        200, json={"outer": {"inner": [{"echo": SECRET}]}}
    ),
    "200-json-list": lambda r: httpx.Response(200, json=[{"echo": SECRET}]),
    "200-non-json-text": lambda r: httpx.Response(200, text=f"Bearer {SECRET}"),
    "201-json": lambda r: httpx.Response(201, json={"echo": SECRET}),
    "401": lambda r: httpx.Response(401, text=f"bad key {SECRET}"),
    "403": lambda r: httpx.Response(403, text=f"bad key {SECRET}"),
    "404": lambda r: httpx.Response(404, text=f"no route for {SECRET}"),
    "503": lambda r: httpx.Response(503, text=f"down, sent {SECRET}"),
    "connect-error": _raiser(
        lambda r: httpx.ConnectError(f"refused {SECRET}", request=r)
    ),
    "timeout": _raiser(lambda r: httpx.ReadTimeout(f"slow {SECRET}", request=r)),
    "http-error": _raiser(lambda r: httpx.HTTPError(f"boom {SECRET}")),
    "non-http-error": _raiser(lambda r: RuntimeError(f"boom {SECRET}")),
}


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


@pytest.mark.parametrize("case", sorted(_LEAK_CASES))
async def test_no_client_response_ever_contains_the_key(monkeypatch, case):
    """Spec: "the bridge never proxies a gateway's API_SERVER_KEY to a caller."
    That claim is unconditional, so this is asserted over every status class and
    every failure path rather than over the one auth code it is easiest to think
    of. A 2xx echoing the key used to satisfy the old single-case version of this
    test while returning the key verbatim."""
    c = _client(monkeypatch, _LEAK_CASES[case])
    got = await c.request("GET", "/health")
    assert SECRET not in repr(got)


@pytest.mark.parametrize("case", sorted(_LEAK_CASES))
async def test_no_tool_response_ever_contains_the_key(monkeypatch, case):
    """The same claim, at the layer a caller actually sees."""
    tools = _tool_surface(monkeypatch, _LEAK_CASES[case])
    got = await tools["hermes_status"](profile="alpha")
    assert SECRET not in repr(got)


async def test_a_2xx_body_echoing_the_key_is_redacted_not_discarded(monkeypatch):
    """Redaction has to be surgical: absence of the key must come from replacing
    it, not from dropping the body a caller asked for."""
    c = _client(monkeypatch, lambda r: httpx.Response(
        200, json={"echo": f"Bearer {SECRET}", "keep": "visible"}
    ))
    got = await c.request("GET", "/health")
    assert got["ok"] is True
    assert got["data"]["echo"] == f"Bearer {REDACTED}"
    assert got["data"]["keep"] == "visible", "redaction disturbed the rest of the body"


@pytest.mark.parametrize("layer", ["client", "tool"])
async def test_a_non_ascii_gateway_key_yields_state_and_discloses_nothing(
    monkeypatch, layer
):
    """httpx cannot ASCII-encode such a key, and the resulting UnicodeEncodeError
    is not an httpx.HTTPError. Unguarded it escapes both request() and _call(),
    reaching the caller as error text naming a character of the key and its exact
    offset. It must come back as ordinary structured state instead."""
    ok = lambda r: httpx.Response(200, json={})  # noqa: E731 - never reached
    if layer == "client":
        got = await _client(monkeypatch, ok, key=NON_ASCII_KEY).request("GET", "/health")
    else:
        tools = _tool_surface(monkeypatch, ok, key=NON_ASCII_KEY)
        got = await tools["hermes_status"](profile="alpha")

    assert got["state"] == "unreachable"
    assert got["reason"] and got["next"], "a failure state still has to say what to do"
    rendered = repr(got)
    for char in sorted(set(NON_ASCII_KEY)):
        assert char not in rendered, f"a character of the key leaked: {char!r}"
    assert not any(c.isdigit() for c in rendered), (
        "a digit here can only be a byte offset into the key"
    )
    assert ALPHA.key_env in got["next"], "the env var NAME is the safe thing to name"


async def test_internal_failures_are_indistinguishable_to_the_caller(monkeypatch):
    """A reason derived from the exception would let a caller probe for which
    internal failure occurred, and would reintroduce the leak the moment a new
    exception type carrying key material appeared. One fixed reason for all."""
    ok = lambda r: httpx.Response(200, json={})  # noqa: E731 - never reached
    encode = await _client(monkeypatch, ok, key=NON_ASCII_KEY).request("GET", "/health")
    runtime = await _client(
        monkeypatch, _raiser(lambda r: RuntimeError("boom"))
    ).request("GET", "/health")
    value = await _client(
        monkeypatch, _raiser(lambda r: ValueError("different boom"))
    ).request("GET", "/health")
    assert encode == runtime == value


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
