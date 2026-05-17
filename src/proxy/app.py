"""FastAPI application factory."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings
from .client import ProviderClient
from .routes import router, set_provider_client

logger = logging.getLogger(__name__)


def _configure_logging(log_file: str):
    if logging.root.handlers:
        return
    os.makedirs(os.path.dirname(log_file) if os.path.dirname(log_file) else ".", exist_ok=True)
    open(log_file, "w", encoding="utf-8").close()
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    for noisy in ("uvicorn", "uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_file)
    logger.info("Starting backdoor → %s (%s)", settings.provider_base_url, settings.provider_model)

    client = ProviderClient(settings)
    set_provider_client(client)

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

    yield

    if tg_app:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

    await client.aclose()
    logger.info("Proxy shut down")


def create_app() -> FastAPI:
    app = FastAPI(title="backdoor", version="1.0.0", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
