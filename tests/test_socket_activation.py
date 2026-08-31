"""Socket activation must fail open everywhere except a real launchd launch.

The property that matters for the rest of the suite: outside launchd this
module returns None and every caller binds host/port exactly as before, so no
other test ever notices activation exists.
"""

import socket

from src.proxy.socket_activation import activated_socket


def test_outside_launchd_returns_none():
    # The test runner was not launched by launchd with a socket named "api",
    # so launch_activate_socket answers ENOENT/ESRCH and we must fail open.
    assert activated_socket("api") is None
    assert activated_socket("forward") is None
    assert activated_socket("no-such-name") is None


def test_forward_proxy_serves_on_a_provided_socket():
    """ForwardProxy must serve on an injected listener instead of binding.

    Stands in for the launchd-activated fd: a pre-made listening socket whose
    owner is not ForwardProxy. The proxy must accept on it and report its port.
    """
    import asyncio

    from src.proxy.ca import LocalCA
    from src.proxy.forward import ForwardProxy

    async def scenario(tmp):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        port = sock.getsockname()[1]

        proxy = ForwardProxy(
            listen_sock=sock,
            mitm_hosts=["api.anthropic.com"],
            ca=LocalCA(tmp),
        )
        await proxy.start()
        try:
            assert proxy.port == port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # A CONNECT for a non-intercepted host is tunneled; just proving
            # the accept loop runs on the injected socket is the point here.
            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(tmp))
