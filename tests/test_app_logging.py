"""Logging must remain bounded when launchd discards stdout and stderr."""

import logging
from logging.handlers import RotatingFileHandler

from src.proxy.app import _configure_logging


def test_configure_logging_adds_a_bounded_file_handler(tmp_path) -> None:
    log_path = tmp_path / "router.log"
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    noisy = ("uvicorn", "uvicorn.access", "httpx", "httpcore")
    before_noisy_levels = {name: logging.getLogger(name).level for name in noisy}

    try:
        _configure_logging(str(log_path))
        added = [handler for handler in root.handlers if handler not in before]

        assert len(added) == 1
        assert isinstance(added[0], RotatingFileHandler)
        assert added[0].maxBytes == 10 * 1024 * 1024
        assert added[0].backupCount == 3

        _configure_logging(str(log_path))
        matching = [
            handler
            for handler in root.handlers
            if isinstance(handler, RotatingFileHandler)
            and handler.baseFilename == str(log_path)
        ]
        assert len(matching) == 1

        second_path = tmp_path / "router-2.log"
        _configure_logging(str(second_path))
        owned_paths = {
            handler.baseFilename
            for handler in root.handlers
            if isinstance(handler, RotatingFileHandler)
            and getattr(handler, "_backdoor_file_handler", False)
        }
        assert owned_paths == {str(second_path)}
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(before_level)
        for name, level in before_noisy_levels.items():
            logging.getLogger(name).setLevel(level)
