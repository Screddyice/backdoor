"""Bounded process logging shared by the launcher and FastAPI lifespan."""

import logging
from logging.handlers import RotatingFileHandler
import os


_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 3
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_OWNED_HANDLER = "_backdoor_file_handler"


def configure_logging(log_file: str) -> None:
    path = os.path.abspath(os.path.expanduser(log_file))
    owned = [
        handler
        for handler in logging.root.handlers
        if getattr(handler, _OWNED_HANDLER, False)
    ]
    matching = next(
        (
            handler
            for handler in owned
            if isinstance(handler, logging.FileHandler)
            and handler.baseFilename == path
        ),
        None,
    )

    for handler in owned:
        compatible = (
            handler is matching
            and isinstance(handler, RotatingFileHandler)
            and handler.maxBytes == _LOG_MAX_BYTES
            and handler.backupCount == _LOG_BACKUP_COUNT
        )
        if not compatible:
            logging.root.removeHandler(handler)
            handler.close()
            if handler is matching:
                matching = None

    os.makedirs(os.path.dirname(path), exist_ok=True)
    if matching is None:
        handler = RotatingFileHandler(
            path,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        setattr(handler, _OWNED_HANDLER, True)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logging.root.addHandler(handler)

    logging.root.setLevel(logging.DEBUG)
    for noisy in ("uvicorn", "uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
