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

        settings = get_settings()
        uvicorn.run(
            "src.proxy.app:app",
            host=settings.host,
            port=settings.port,
            log_config=None,
        )
    except BaseException as exc:
        if not isinstance(exc, KeyboardInterrupt):
            logger.exception("backdoor failed during startup")
        raise


if __name__ == "__main__":
    main()
