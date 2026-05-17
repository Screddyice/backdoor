"""Telegram bot for remotely triggering Claude Code sessions."""

import logging
import uuid

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

from ..config import Settings
from .session import SessionManager

logger = logging.getLogger(__name__)

_manager: SessionManager | None = None
_allowed_id: int | None = None


def _guard(update: Update) -> bool:
    if not update.effective_user or _allowed_id is None:
        return False
    return update.effective_user.id == _allowed_id


async def _cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
    assert update.message
    await update.message.reply_text(
        "Backdoor bot ready.\n\n"
        "Send any message to run it as a Claude Code prompt.\n"
        "Commands: /stop — cancel running sessions."
    )


async def _cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update) or not _manager:
        return
    assert update.message
    await _manager.stop_all()
    await update.message.reply_text("Stopped all sessions.")


async def _on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update) or not _manager:
        return
    assert update.message and update.message.text

    prompt = update.message.text
    session_id = uuid.uuid4().hex[:8]
    status_msg = await update.message.reply_text(f"[{session_id}] Starting…")

    buffer: list[str] = []

    async def on_line(sid: str, line: str):
        buffer.append(line)
        # Flush every 20 lines to avoid Telegram rate limits
        if len(buffer) % 20 == 0:
            snippet = "\n".join(buffer[-20:])
            try:
                await status_msg.edit_text(f"[{sid}] Running…\n```\n{snippet[-3000:]}\n```", parse_mode=ParseMode.MARKDOWN_V2)
            except Exception:
                pass

    async def on_done(sid: str):
        result = "\n".join(buffer)[-4000:] or "(no output)"
        try:
            await status_msg.edit_text(f"[{sid}] Done\n```\n{result}\n```", parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            await update.message.reply_text(f"[{sid}] Done. Output truncated — check logs.")

    started = await _manager.run(session_id, prompt, on_line, on_done)
    if not started:
        await status_msg.edit_text("Too many active sessions. Try again later.")


async def build_telegram_app(settings: Settings) -> Application:
    global _manager, _allowed_id
    _allowed_id = settings.telegram_allowed_user_id
    _manager = SessionManager(
        workspace=settings.claude_workspace,
        api_url=f"http://127.0.0.1:{settings.port}/v1",
        max_sessions=settings.max_cli_sessions,
    )

    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("stop", _cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    return app
