"""CLI session tracking for Telegram-triggered Claude Code runs."""

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Session:
    id: str
    process: asyncio.subprocess.Process | None = None
    output: list[str] = field(default_factory=list)
    done: bool = False


class SessionManager:
    def __init__(self, workspace: str, api_url: str, max_sessions: int = 5):
        self.workspace = workspace
        self.api_url = api_url
        self.max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def run(self, session_id: str, prompt: str, on_line, on_done) -> bool:
        async with self._lock:
            if len(self._sessions) >= self.max_sessions:
                return False
            session = Session(id=session_id)
            self._sessions[session_id] = session

        os.makedirs(self.workspace, exist_ok=True)
        env = {**os.environ, "ANTHROPIC_BASE_URL": self.api_url, "ANTHROPIC_API_KEY": "proxy"}

        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--print", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.workspace,
                env=env,
            )
            session.process = proc

            assert proc.stdout
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                session.output.append(line)
                await on_line(session_id, line)

            await proc.wait()
        except Exception:
            logger.exception("Session %s failed", session_id)
        finally:
            session.done = True
            async with self._lock:
                self._sessions.pop(session_id, None)
            await on_done(session_id)

        return True

    async def stop(self, session_id: str):
        session = self._sessions.get(session_id)
        if session and session.process:
            session.process.terminate()

    async def stop_all(self):
        for sid in list(self._sessions):
            await self.stop(sid)
