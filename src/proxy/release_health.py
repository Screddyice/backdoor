"""Process identity and active HTTP work for the local release controller."""
from pathlib import Path
import subprocess


def startup_revision():
    try:
        return subprocess.check_output(
            ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "HEAD"],
            text=True, timeout=3, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


REVISION = startup_revision()


class ActiveRequests:
    """Count a request until its ASGI stream finishes, including disconnects."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            return await self.app(scope, receive, send)
        state = scope["app"].state
        state.active_requests += 1
        try:
            await self.app(scope, receive, send)
        finally:
            state.active_requests -= 1
