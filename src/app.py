"""Bot entry point: handler registration + polling loop."""

import asyncio
import logging
from pathlib import Path

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

BOT_COMMANDS = [
    BotCommand("new", "Freund-Session neu starten (auch Lehrer zurücksetzen)"),
    BotCommand("new_friend", "Nur Freund-Session neu starten"),
    BotCommand("new_teacher", "Lehrer-Session zurücksetzen"),
    BotCommand("teacher", "Frage an den Lehrer stellen"),
    BotCommand("level", "Dein Deutsch-Niveau setzen (A1–C2)"),
    BotCommand("rewind", "Letzte Freund-Nachrichten zurückspulen"),
    BotCommand("reload", "CLAUDE.md für beide Personas neu laden"),
    BotCommand("reload_friend", "CLAUDE.md des Freundes neu laden"),
    BotCommand("reload_teacher", "CLAUDE.md des Lehrers neu laden"),
    BotCommand("status", "Aktuellen Sitzungsstatus anzeigen"),
    BotCommand("ping", "Erreichbarkeit prüfen"),
]

# Groups run teacher-only — friend/rewind/onboarding commands are hidden there.
GROUP_BOT_COMMANDS = [
    BotCommand("teacher", "Frage an den Lehrer stellen"),
    BotCommand("new_teacher", "Lehrer-Session zurücksetzen"),
    BotCommand("level", "Dein Deutsch-Niveau setzen (A1–C2)"),
    BotCommand("status", "Aktuellen Sitzungsstatus anzeigen"),
    BotCommand("ping", "Erreichbarkeit prüfen"),
]

from .config import (
    ALLOWED_USERS,
    BOT_TOKEN,
    CFG,
    FRIEND_MODEL,
    FRIEND_WORKDIR,
    TEACHER_MODEL,
    TEACHER_WORKDIR,
    VOICE_ENABLED,
)
from .handlers import (
    handle_message,
    level_cmd,
    new_teacher_cmd,
    ping_cmd,
    reload_cmd,
    reload_friend_cmd,
    reload_teacher_cmd,
    rewind_callback,
    rewind_cmd,
    status_cmd,
    teacher_cmd,
)
from .idle import idle_pinger
from .onboarding import build_handler as build_onboarding_handler
from .voice import handle_voice

logger = logging.getLogger(__name__)


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            f"Set {CFG['bot']['token_env']} (or TELEGRAM_BOT_TOKEN) in .env"
        )
    for wd in (FRIEND_WORKDIR, TEACHER_WORKDIR):
        if not (Path(wd) / "CLAUDE.md").exists():
            raise SystemExit(f"Missing {Path(wd) / 'CLAUDE.md'}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(build_onboarding_handler())
    app.add_handler(CommandHandler("teacher", teacher_cmd))
    app.add_handler(CommandHandler("new_teacher", new_teacher_cmd))
    app.add_handler(CommandHandler("level", level_cmd))
    app.add_handler(CommandHandler("reload_friend", reload_friend_cmd))
    app.add_handler(CommandHandler("reload_teacher", reload_teacher_cmd))
    app.add_handler(CommandHandler("reload", reload_cmd))
    app.add_handler(CommandHandler("rewind", rewind_cmd))
    app.add_handler(CallbackQueryHandler(rewind_callback, pattern=r"^rw:"))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def on_startup(application: Application):
        await application.bot.set_my_commands(
            BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats()
        )
        await application.bot.set_my_commands(
            GROUP_BOT_COMMANDS, scope=BotCommandScopeAllGroupChats()
        )
        asyncio.create_task(idle_pinger(application))

    app.post_init = on_startup

    async def err_handler(update, context):
        logger.error("Unhandled exception", exc_info=context.error)

    app.add_error_handler(err_handler)

    print("German Language Companion starting…")
    print("  Freund-Workdir:", FRIEND_WORKDIR)
    print("  Lehrer-Workdir:", TEACHER_WORKDIR)
    print("  Freund-Model  :", FRIEND_MODEL or "(default)")
    print("  Lehrer-Model  :", TEACHER_MODEL or "(default)")
    print("  Allowed users :", ALLOWED_USERS or "everyone")
    print("  Voice         :", "on" if VOICE_ENABLED else "off")
    app.run_polling(drop_pending_updates=True)
