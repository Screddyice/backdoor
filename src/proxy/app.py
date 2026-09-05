"""FastAPI application factory."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from .config import get_settings
from .resolver import install as install_dns_cache
from .client import ProviderClient
from .logging_config import configure_logging as _configure_logging
from .routes import failover_recovery_loop, router, set_provider_client
from .codex_routes import codex_router, close_codex_clients

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_file)
    # Before any client is built: a gateway that stops answering DNS while it
    # keeps routing is the single most common upstream failure on this host
    # (722 of 731 transport failures in the log), and a remembered address gets
    # the request through without the breaker having to claim the GPU.
    install_dns_cache()
    logger.info("Starting backdoor → %s (%s)", settings.provider_base_url, settings.provider_model)

    client = ProviderClient(settings)
    set_provider_client(client)

    # Ticker that closes a stuck-open failover breaker. It exists because the
    # breaker's half-open path needs a real request to ride on, and an outage is
    # what stops those arriving — see routes.failover_recovery_loop. Only
    # meaningful when failover can happen at all.
    recovery = None
    if settings.failover_to_local:
        recovery = asyncio.create_task(failover_recovery_loop(settings))

    # Start Telegram bot if configured
    tg_app = None
    if settings.telegram_bot_token:
        try:
            from .telegram.bot import build_telegram_app
            tg_app = await build_telegram_app(settings)
            await tg_app.initialize()
            await tg_app.start()
            await tg_app.updater.start_polling()
            logger.info("Telegram bot started")
        except Exception:
            logger.exception("Telegram bot failed to start")

    # Start the CONNECT forward proxy if configured. It shares this event loop
    # and splices intercepted traffic back into this same uvicorn over loopback,
    # so it needs no separate service to supervise.
    #
    # A failure here must not take the router down: without the forward proxy a
    # session simply falls back to whatever ANTHROPIC_BASE_URL says, which is
    # degraded but working. Killing the router instead would take out inference
    # for every session on the machine.
    forward = None
    if settings.forward_proxy:
        try:
            from .ca import LocalCA
            from .forward import ForwardProxy

            forward = ForwardProxy(
                listen_host=settings.forward_host,
                listen_port=settings.forward_port,
                mitm_hosts=settings.forward_mitm_hosts.split(","),
                router_host=settings.forward_router_host,
                router_port=settings.forward_router_port,
                ca=LocalCA(settings.forward_ca_dir),
                idle_timeout=settings.forward_idle_timeout,
                max_connections=settings.forward_max_connections,
            )
            await forward.start()
            logger.info(
                "Forward proxy ready — HTTPS_PROXY=http://%s:%d, "
                "NODE_EXTRA_CA_CERTS=%s",
                settings.forward_host,
                forward.port,
                forward.ca.ca_cert_path,
            )
        except Exception:
            logger.exception("Forward proxy failed to start; continuing without it")
            forward = None

    yield

    if recovery:
        recovery.cancel()
        with suppress(asyncio.CancelledError):
            await recovery

    if forward:
        await forward.stop()

    if tg_app:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

    await client.aclose()
    await close_codex_clients()
    logger.info("Proxy shut down")


def create_app() -> FastAPI:
    app = FastAPI(title="backdoor", version="1.0.0", lifespan=lifespan)
    # Mount before the Anthropic catch-all route so Codex Responses never fall
    # through to api.anthropic.com.
    app.include_router(codex_router)
    app.include_router(router)
    return app


app = create_app()
