"""Launch uvicorn after bounded logging exists, including startup failures."""

import logging
import os

from .logging_config import configure_logging


logger = logging.getLogger(__name__)


def main() -> None:
    configure_logging(os.environ.get("LOG_FILE", "proxy.log"))
    try:
        import uvicorn

        from .config import get_settings
        from .socket_activation import activated_socket

        settings = get_settings()
        # With a `Sockets` key in the LaunchAgent, launchd owns the listener
        # and hands us a duplicated fd. launchd's own reference stays open
        # across restarts, so connections queue in the backlog instead of
        # being refused while this process is down or draining. detach():
        # uvicorn adopts the fd, so exactly one owner closes it.
        api_sock = activated_socket("api")
        listener = (
            {"fd": api_sock.detach()}
            if api_sock is not None
            else {"host": settings.host, "port": settings.port}
        )
        uvicorn.run(
            "src.proxy.app:app",
            **listener,
            log_config=None,
            # SIGTERM drains in-flight requests, but uvicorn closes the LISTENER
            # at the start of the drain and launchd only respawns after exit —
            # so an unbounded drain is an unbounded connection-refused window.
            # A Claude SSE stream can run for minutes, and on 2026-08-31 one
            # held a restart open ~90s while every other session got
            # "[Errno 61] Connection refused". 20s lets short responses finish
            # and caps what any one stream can cost everyone else; clients
            # already retry through a window that size.
            timeout_graceful_shutdown=20,
        )
    except BaseException as exc:
        if not isinstance(exc, KeyboardInterrupt):
            logger.exception("backdoor failed during startup")
        raise


if __name__ == "__main__":
    main()
