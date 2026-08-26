"""A passthrough stream that dies AFTER headers must be logged and counted.

Regression cover for the 2026-08-26 23:17 and 23:47 incidents. Both rendered in
Claude Code as `The response stopped arriving. The response above may be
incomplete.`, and for both the router log contains not one line about the failed
turn — the last thing it says is the `→ passthrough` that started it.

`_try_upstream` guards only up to the headers-received stage. Past that the
response was handed to `StreamingResponse(uresp.aiter_raw())` bare, so a
transport error inside the body escaped to uvicorn (which answers mid-response
by dropping the connection) and never reached `record_failure`.

The lost request cannot be recovered — headers are already on the wire, so there
is nothing left to fail over. What these tests pin is the part that was fixable:
the failure is now visible in the log, and it feeds the breaker, so the retry
that follows seconds later can be served locally instead of truncating again.
"""

import json

import httpx
import pytest

import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings


class DyingStream(httpx.AsyncByteStream):
    """Headers, some body, then the connection goes away."""

    def __init__(self, chunk: bytes, exc: BaseException):
        self._chunk = chunk
        self._exc = exc

    async def __aiter__(self):
        yield self._chunk
        raise self._exc


def _upstream_that_dies(exc: BaseException) -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=DyingStream(b'event: ping\ndata: {"type": "ping"}\n\n', exc),
        )

    return httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(handler),
    )


def _app(**overrides):
    routes._breaker = None  # rebuilt from these settings
    app = create_app()
    kwargs = {
        "router_mode": "hybrid",
        "failover_to_local": True,
        # High enough that one failure cannot open the breaker: these tests are
        # about the failure being SEEN and COUNTED, not about opening.
        "failover_threshold": 99,
    }
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    app.dependency_overrides[get_settings] = lambda: settings
    return app, settings


@pytest.fixture
def dying_app(request):
    exc = getattr(request, "param", httpx.ReadTimeout("timed out"))
    routes._upstream_client = _upstream_that_dies(exc)
    app, settings = _app()
    try:
        yield app, settings
    finally:
        app.dependency_overrides.clear()
        routes._upstream_client = None
        routes._breaker = None


_BODY = {
    "model": "claude-opus-5",
    "max_tokens": 16,
    "stream": True,
    "messages": [{"role": "user", "content": "hi"}],
}


async def _post(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with client.stream(
            "POST", "/v1/messages",
            content=json.dumps(_BODY).encode(),
            headers={"content-type": "application/json"},
        ) as resp:
            body = b""
            async for chunk in resp.aiter_raw():
                body += chunk
            return resp, body


@pytest.mark.asyncio
async def test_midstream_death_is_counted_by_the_breaker(dying_app):
    app, settings = dying_app
    with pytest.raises(Exception):
        await _post(app)

    br = routes.get_breaker(settings)
    # The whole point: the breaker heard about it. Before the fix this was 0,
    # so an outage that only ever killed streams mid-flight looked, to the
    # breaker, exactly like an outage that was not happening.
    assert br._failures == 1
    assert br.reason == "ReadTimeout"


@pytest.mark.asyncio
async def test_midstream_death_is_logged(dying_app, caplog):
    app, _ = dying_app
    with caplog.at_level("WARNING", logger="src.proxy.routes"):
        with pytest.raises(Exception):
            await _post(app)

    hits = [r.getMessage() for r in caplog.records if "mid-response" in r.getMessage()]
    assert hits, f"no mid-stream warning logged; got {[r.getMessage() for r in caplog.records]}"
    # Byte count included on purpose: "died after 0 bytes" and "died after 40KB"
    # are different failures, and the log is the only place that shows which.
    assert "byte(s)" in hits[0]
    assert "ReadTimeout" in hits[0]


@pytest.mark.asyncio
async def test_client_disconnect_is_not_counted():
    """A client hanging up is not evidence against Anthropic.

    Counting it would let a user pressing Ctrl-C push the breaker toward
    claiming the GPU, which is the opposite of what the connectivity probe is
    there to prevent. `CancelledError` is a BaseException and `GeneratorExit`
    is not an `httpx.TransportError`, so neither is caught — this pins that.
    """
    routes._breaker = None
    settings = Settings(router_mode="hybrid", failover_to_local=True, failover_threshold=99)
    br = routes.get_breaker(settings)

    resp = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=DyingStream(b"data: x\n\n", RuntimeError("client went away")),
    )
    body = routes._relay_body(resp, settings)
    assert await body.__anext__() == b"data: x\n\n"
    with pytest.raises(RuntimeError):
        await body.__anext__()

    assert br._failures == 0, "a non-transport error must not count as an outage"
    routes._breaker = None
