import asyncio
from types import SimpleNamespace
import pytest
from src.proxy.release_health import ActiveRequests, REVISION


@pytest.mark.asyncio
async def test_active_request_spans_stream_and_cleans_up_on_disconnect():
    state = SimpleNamespace(active_requests=0)
    started, finish = asyncio.Event(), asyncio.Event()
    async def stream(scope, receive, send):
        started.set()
        await finish.wait()
        raise ConnectionError('client left')
    scope = {'type': 'http', 'path': '/v1/messages', 'app': SimpleNamespace(state=state)}
    task = asyncio.create_task(ActiveRequests(stream)(scope, None, None))
    await started.wait()
    assert state.active_requests == 1
    finish.set()
    with pytest.raises(ConnectionError):
        await task
    assert state.active_requests == 0


@pytest.mark.asyncio
async def test_health_poll_does_not_count_as_work():
    state = SimpleNamespace(active_requests=0)
    async def endpoint(scope, receive, send):
        assert state.active_requests == 0
    await ActiveRequests(endpoint)({'type': 'http', 'path': '/health', 'app': SimpleNamespace(state=state)}, None, None)


def test_revision_is_captured_at_import():
    assert len(REVISION) == 40
