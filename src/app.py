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
    BotCommand("profile", "Gesprächspartner (Freund) wählen/wechseln"),
    BotCommand("setup", "Aktuellen Freund einrichten"),
    BotCommand("reset", "Aktuellen Freund zurücksetzen"),
    BotCommand("reset_teacher", "Lehrer-Session zurücksetzen"),
    BotCommand("teacher", "Frage an den Lehrer stellen"),
    BotCommand("level", "Dein Deutsch-Niveau setzen (A1–C2)"),
    BotCommand("rewind", "Letzte Freund-Nachrichten zurückspulen"),
    BotCommand("reload", "CLAUDE.md für beide Personas neu laden"),
    BotCommand("reload_friend", "CLAUDE.md des Freundes neu laden"),
    BotCommand("reload_teacher", "CLAUDE.md des Lehrers neu laden"),
    BotCommand("status", "Aktuellen Sitzungsstatus anzeigen"),
    BotCommand("ping", "Erreichbarkeit prüfen"),
]

# Groups run teacher-only — friend/rewind/profile commands are hidden there.
GROUP_BOT_COMMANDS = [
    BotCommand("teacher", "Frage an den Lehrer stellen"),
    BotCommand("reset_teacher", "Lehrer-Session zurücksetzen"),
    BotCommand("level", "Dein Deutsch-Niveau setzen (A1–C2)"),
    BotCommand("status", "Aktuellen Sitzungsstatus anzeigen"),
    BotCommand("ping", "Erreichbarkeit prüfen"),
]

from .config import (
    ALLOWED_USERS,
    BOT_TOKEN,
    CFG,
    DEFAULT_FRIEND_PROFILE,
    FRIEND_BASE_WORKDIR,
    FRIEND_PROFILES,
    TEACHER_MODEL,
    TEACHER_WORKDIR,
    VOICE_ENABLED,
)
from .handlers import (
    handle_message,
    level_cmd,
    ping_cmd,
    profile_callback,
    profile_cmd,
    reload_cmd,
    reload_friend_cmd,
    reload_teacher_cmd,
    reset_callback,
    reset_cmd,
    reset_teacher_cmd,
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
    if not FRIEND_PROFILES:
        raise SystemExit(
            f"No friend profiles found under {FRIEND_BASE_WORKDIR} — each profile "
            "needs a subfolder with both CLAUDE.md and profile.yaml."
        )
    if DEFAULT_FRIEND_PROFILE not in FRIEND_PROFILES:
        raise SystemExit(
            f"default_profile '{DEFAULT_FRIEND_PROFILE}' not found; "
            f"available: {list(FRIEND_PROFILES)}"
        )
    for pid, meta in FRIEND_PROFILES.items():
        if not (Path(meta.workdir) / "CLAUDE.md").exists():
            raise SystemExit(f"Missing CLAUDE.md for friend profile '{pid}'")
    if not (Path(TEACHER_WORKDIR) / "CLAUDE.md").exists():
        raise SystemExit(f"Missing {Path(TEACHER_WORKDIR) / 'CLAUDE.md'}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(build_onboarding_handler())
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CallbackQueryHandler(profile_callback, pattern=r"^prof:"))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CallbackQueryHandler(reset_callback, pattern=r"^rstf:"))
    app.add_handler(CommandHandler("teacher", teacher_cmd))
    app.add_handler(CommandHandler("reset_teacher", reset_teacher_cmd))
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
    print("  Friend base   :", FRIEND_BASE_WORKDIR)
    print("  Friend profiles:", ", ".join(
        f"{pid}{' (default)' if pid == DEFAULT_FRIEND_PROFILE else ''}"
        for pid in FRIEND_PROFILES
    ) or "(none)")
    print("  Lehrer-Workdir:", TEACHER_WORKDIR)
    print("  Lehrer-Model  :", TEACHER_MODEL or "(default)")
    print("  Allowed users :", ALLOWED_USERS or "everyone")
    print("  Voice         :", "on" if VOICE_ENABLED else "off")
    app.run_polling(drop_pending_updates=True)
