"""Telegram message dispatch: routes text / transcribed voice to the friend +
teacher personas, handles reply-threading, and manages the 'typing…' keeper."""

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, ContextTypes

from ..config import TEACHER_GROUP_LEVEL
from ..friend import (
    PENDING_FRIEND,
    PendingFriend,
    USER_LOCKS,
    friend_turn,
)
from ..sessions import load_user, now_iso, profile_state, save_user
from ..teacher import GROUP_KONTEXT_LABEL, teacher_followup_turn, teacher_turn
from .common import is_allowed, is_allowed_chat, is_group_chat

logger = logging.getLogger(__name__)

INVALID_REPLY_MSG = (
    "❌ Antworten gehen nur an die letzte Lehrer-Korrektur. "
    "Schreib mir normal, oder antworte gezielt auf die letzte Korrektur."
)

CHAT_ACTION_REFRESH_S = 4


async def chat_action_keeper(
    app: Application,
    chat_id: int,
    friend_done: asyncio.Event,
    teacher_done: asyncio.Event,
) -> None:
    """Show 'typing…' until the friend reply lands, then 'choose_sticker…'
    until the teacher finishes. One keeper per dispatch."""
    while not friend_done.is_set():
        try:
            await app.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(friend_done.wait(), timeout=CHAT_ACTION_REFRESH_S)
        except asyncio.TimeoutError:
            pass
    while not teacher_done.is_set():
        try:
            await app.bot.send_chat_action(chat_id=chat_id, action="choose_sticker")
        except Exception:
            pass
        try:
            await asyncio.wait_for(teacher_done.wait(), timeout=CHAT_ACTION_REFRESH_S)
        except asyncio.TimeoutError:
            pass


QUOTE_MAX = 500


def _friend_text_with_quote(text: str, quoted: str) -> str:
    q = quoted.strip()[:QUOTE_MAX]
    return f"[Antwort auf deine frühere Nachricht: „{q}“]\n{text}"


async def dispatch_user_text(app: Application, user_id: int, chat_id: int,
                              message_id: int, text: str,
                              quoted: str | None = None) -> None:
    user = load_user(user_id)
    user["chat_id"] = chat_id
    user["last_message_ts"] = now_iso()
    user["idle_ping_sent_at"] = None
    save_user(user_id, user)

    # The friend sees a reference to the quoted message; the teacher keeps
    # evaluating the user's plain text against the latest friend reply.
    friend_text = _friend_text_with_quote(text, quoted) if quoted else text

    friend_done = asyncio.Event()
    teacher_done = asyncio.Event()

    async def _run_friend():
        try:
            await friend_turn(app, user_id, chat_id)
        finally:
            friend_done.set()

    async def _run_teacher():
        try:
            await teacher_turn(app, user_id, chat_id, message_id, text)
        finally:
            teacher_done.set()

    async with USER_LOCKS[user_id]:
        pending = PENDING_FRIEND.get(user_id)
        if pending is not None:
            queued = list(pending.queued) + [friend_text]
            excerpt = pending.excerpt or (text[:60])
            proc = pending.proc_holder.get("proc")
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            if pending.task is not None and not pending.task.done():
                pending.task.cancel()
            if pending.keeper_task is not None and not pending.keeper_task.done():
                pending.keeper_task.cancel()
        else:
            queued = [friend_text]
            excerpt = text[:60]

        new_pending = PendingFriend(queued=queued, excerpt=excerpt)
        PENDING_FRIEND[user_id] = new_pending
        new_pending.task = asyncio.create_task(_run_friend())
        new_pending.keeper_task = asyncio.create_task(
            chat_action_keeper(app, chat_id, friend_done, teacher_done)
        )

    asyncio.create_task(_run_teacher())


async def _run_with_teacher_keeper(app: Application, chat_id: int, coro) -> None:
    """Run a teacher-only coroutine while showing the 'choose_sticker' action.
    The friend event is pre-set so the keeper skips straight to the teacher
    phase — used for follow-ups and for all group-chat turns (no friend)."""
    friend_done = asyncio.Event()
    friend_done.set()
    teacher_done = asyncio.Event()
    keeper = asyncio.create_task(
        chat_action_keeper(app, chat_id, friend_done, teacher_done)
    )
    try:
        await coro
    finally:
        teacher_done.set()
        try:
            await keeper
        except asyncio.CancelledError:
            pass


async def _run_followup_with_keeper(app: Application, user_id: int, chat_id: int,
                                      message_id: int, text: str,
                                      state_chat_id: int | None = None,
                                      quoted_correction: str | None = None) -> None:
    await _run_with_teacher_keeper(
        app, chat_id,
        teacher_followup_turn(app, user_id, chat_id, message_id, text,
                              state_chat_id, quoted_correction=quoted_correction),
    )


async def handle_user_text(app: Application, update: Update, text: str) -> None:
    """Shared entry point for text and transcribed voice. Routes replies-to-
    teacher to a follow-up turn, rejects replies to anything else, otherwise
    performs the normal dual dispatch."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    replied = update.message.reply_to_message

    if replied is not None:
        user = load_user(user_id)
        last_teacher_id = user.get("last_teacher_message_id")
        teacher_ids = user.get("teacher_message_ids") or []
        is_teacher_reply = replied.message_id in teacher_ids or (
            last_teacher_id and replied.message_id == last_teacher_id
        )
        if is_teacher_reply:
            # When the user replies to an older correction (not the latest one),
            # quote it so the teacher knows which correction the follow-up means.
            quoted_correction = None
            if replied.message_id != last_teacher_id:
                quoted_correction = (replied.text or "").strip() or None
            asyncio.create_task(
                _run_followup_with_keeper(
                    app, user_id, chat_id, message_id, text,
                    quoted_correction=quoted_correction,
                )
            )
            return
        friend_ids = profile_state(user).get("message_ids") or []
        if replied.message_id in friend_ids:
            # Replying to the friend's latest message is just the natural next
            # turn — dispatch normally, no quote. Replying to an older friend
            # message: tell the friend which message is being answered.
            quoted = None
            if replied.message_id != friend_ids[-1]:
                quoted = (replied.text or "").strip() or None
            await dispatch_user_text(
                app, user_id, chat_id, message_id, text, quoted=quoted
            )
            return
        await update.message.reply_text(INVALID_REPLY_MSG)
        return

    await dispatch_user_text(app, user_id, chat_id, message_id, text)


async def handle_group_text(app: Application, update: Update, text: str) -> None:
    """Teacher-only entry point for group chats. The friend persona never runs.
    Each member's teacher state is isolated per (group, user). Corrects every
    member message inline (or 👀 when fine); replies to another member's
    message use that message as context; replies to the teacher's own last
    correction become a follow-up."""
    sender = update.effective_user
    if sender is None or sender.is_bot or not text:
        return
    user_id = sender.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    replied = update.message.reply_to_message

    user = load_user(user_id, chat_id)
    last_teacher_id = user.get("last_teacher_message_id")
    teacher_ids = user.get("teacher_message_ids") or []
    # Per-member /level choice wins; otherwise the configured group default.
    level = user.get("user_level") or TEACHER_GROUP_LEVEL

    if replied is not None:
        replied_from = replied.from_user
        is_teacher_reply = replied_from is not None and replied_from.is_bot and (
            replied.message_id in teacher_ids
            or (last_teacher_id and replied.message_id == last_teacher_id)
        )
        if is_teacher_reply:
            # Member is following up on one of their own corrections. Quote the
            # specific one when it isn't the latest.
            quoted_correction = None
            if replied.message_id != last_teacher_id:
                quoted_correction = (replied.text or "").strip() or None
            asyncio.create_task(_run_followup_with_keeper(
                app, user_id, chat_id, message_id, text, state_chat_id=chat_id,
                quoted_correction=quoted_correction,
            ))
            return
        # Replying to another human (or an older bot message): treat the
        # replied-to text as conversational context for the correction.
        context_text = (replied.text or "").strip() or None
        asyncio.create_task(_run_with_teacher_keeper(
            app, chat_id,
            teacher_turn(app, user_id, chat_id, message_id, text,
                         state_chat_id=chat_id, context_text=context_text,
                         level=level, kontext_label=GROUP_KONTEXT_LABEL),
        ))
        return

    asyncio.create_task(_run_with_teacher_keeper(
        app, chat_id,
        teacher_turn(app, user_id, chat_id, message_id, text,
                     state_chat_id=chat_id, level=level,
                     kontext_label=GROUP_KONTEXT_LABEL),
    ))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        if not is_allowed_chat(update):
            return  # stay silent in non-allowlisted groups
        await handle_group_text(context.application, update, update.message.text)
        return
    if not is_allowed(update):
        return  # stay silent for unauthorized users; only /start responds
    await handle_user_text(context.application, update, update.message.text)
