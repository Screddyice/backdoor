"""The service launcher configures logging before uvicorn starts."""

import logging

import uvicorn

from src.proxy.config import clear_settings_cache
from src.proxy.serve import main


def test_launcher_uses_environment_and_preconfigures_logging(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "startup.log"
    monkeypatch.setenv("LOG_FILE", str(log_path))
    monkeypatch.setenv("HOST", "127.0.0.9")
    monkeypatch.setenv("PORT", "8123")
    seen = {}

    def fake_run(app, **kwargs):
        seen.update(app=app, **kwargs)
        assert log_path.exists()

    monkeypatch.setattr(uvicorn, "run", fake_run)
    clear_settings_cache()
    try:
        main()
        assert seen == {
            "app": "src.proxy.app:app",
            "host": "127.0.0.9",
            "port": 8123,
            "log_config": None,
        }
    finally:
        clear_settings_cache()
        for handler in list(logging.root.handlers):
            if getattr(handler, "_backdoor_file_handler", False):
                logging.root.removeHandler(handler)
                handler.close()
