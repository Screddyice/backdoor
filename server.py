"""Entry point. Run with: uv run uvicorn server:app --host 127.0.0.1 --port 8082"""

from src.proxy.app import app, create_app  # noqa: F401

if __name__ == "__main__":
    import uvicorn
    from src.proxy.config import get_settings

    s = get_settings()
    uvicorn.run("server:app", host=s.host, port=s.port, reload=False, log_level="warning")
