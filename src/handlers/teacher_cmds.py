"""Teacher-session commands: /teacher (ask a follow-up), /reset_teacher, /level.
Work in private chats and in groups (scoped per member)."""

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from ..sessions import VALID_LEVELS, load_user, save_user
from .common import _state_chat_id, _teacher_authorized
from .dispatch import _run_followup_with_keeper


async def teacher_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _teacher_authorized(update):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text(
            "Benutzung: /teacher <Frage oder Nachricht an den Lehrer>"
        )
        return
    asyncio.create_task(_run_followup_with_keeper(
        context.application,
        update.effective_user.id,
        update.effective_chat.id,
        update.message.message_id,
        text,
        state_chat_id=_state_chat_id(update),
    ))


async def reset_teacher_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _teacher_authorized(update):
        return
    state_chat_id = _state_chat_id(update)
    user = load_user(update.effective_user.id, state_chat_id)
    user["teacher"] = None
    user["last_teacher_message_id"] = None
    user["teacher_message_ids"] = []
    save_user(update.effective_user.id, user, state_chat_id)
    await update.message.reply_text("Lehrer-Session zurückgesetzt.")


async def level_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the CEFR level the teacher grades against, then reset the teacher
    session so the new calibration starts clean. Works in private chats and in
    groups (scoped per member)."""
    if not _teacher_authorized(update):
        return
    arg = (context.args[0] if context.args else "").strip().upper()
    if arg not in VALID_LEVELS:
        levels = " / ".join(sorted(VALID_LEVELS))
        await update.message.reply_text(f"Benutzung: /level <{levels}>")
        return
    state_chat_id = _state_chat_id(update)
    user_id = update.effective_user.id
    user = load_user(user_id, state_chat_id)
    user["user_level"] = arg
    user["teacher"] = None
    user["last_teacher_message_id"] = None
    user["teacher_message_ids"] = []
    save_user(user_id, user, state_chat_id)
    await update.message.reply_text(
        f"Niveau auf {arg} gesetzt. Lehrer-Session zurückgesetzt — "
        "die nächste Nachricht wird auf diesem Niveau korrigiert."
    )
