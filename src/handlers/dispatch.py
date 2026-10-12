"""Telegram message dispatch: routes text / transcribed voice / photos to the
friend + teacher personas, handles reply-threading, and manages the 'typing…'
keeper."""

import asyncio
import logging
from pathlib import Path

from telegram import Message, Update
from telegram.ext import Application, ContextTypes

from ..config import PHOTOS_ENABLED, TEACHER_GROUP_LEVEL
from ..friend import (
    PENDING_FRIEND,
    PendingFriend,
    USER_LOCKS,
    friend_turn,
)
from ..photos import download_user_photo, stage, take_staged
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


def _quoted_text(replied: Message) -> str | None:
    """What a reply refers to: the text, a photo's caption, or a photo stub."""
    text = (replied.text or replied.caption or "").strip()
    if text:
        return text
    return "[ein Foto]" if replied.photo else None


def _queue_friend_turn(app: Application, user_id: int, chat_id: int,
                        teacher_done: asyncio.Event, friend_text: str | None,
                        excerpt: str, images: list[Path],
                        media_group_id: str | None = None) -> None:
    """Start a friend turn. A turn still in flight is cancelled and folded in
    (its texts and photos come first). Caller holds USER_LOCKS[user_id]."""
    friend_done = asyncio.Event()

    async def _run_friend():
        try:
            await friend_turn(app, user_id, chat_id)
        finally:
            friend_done.set()

    queued = [friend_text] if friend_text is not None else []
    pending = PENDING_FRIEND.get(user_id)
    if pending is not None:
        queued = list(pending.queued) + queued
        images = list(pending.images) + images
        excerpt = pending.excerpt or excerpt
        media_group_id = media_group_id or pending.media_group_id
        if pending.task is not None and not pending.task.done():
            pending.task.cancel()
        if pending.keeper_task is not None and not pending.keeper_task.done():
            pending.keeper_task.cancel()

    new_pending = PendingFriend(queued=queued, excerpt=excerpt, images=images,
                                media_group_id=media_group_id)
    PENDING_FRIEND[user_id] = new_pending
    new_pending.task = asyncio.create_task(_run_friend())
    new_pending.keeper_task = asyncio.create_task(
        chat_action_keeper(app, chat_id, friend_done, teacher_done)
    )


async def dispatch_user_text(app: Application, user_id: int, chat_id: int,
                              message_id: int, text: str,
                              quoted: str | None = None,
                              images: list[Path] | None = None,
                              media_group_id: str | None = None) -> None:
    user = load_user(user_id)
    user["chat_id"] = chat_id
    user["last_message_ts"] = now_iso()
    user["idle_ping_sent_at"] = None
    save_user(user_id, user)

    # The friend sees a reference to the quoted message; the teacher keeps
    # evaluating the user's plain text against the latest friend reply.
    friend_text = _friend_text_with_quote(text, quoted) if quoted else text
    # Uncaptioned photos sent earlier ride along with this message.
    images = take_staged(user_id) + list(images or [])
    excerpt = ("📷 " if images else "") + text[:60]

    teacher_done = asyncio.Event()

    async def _run_teacher():
        try:
            await teacher_turn(app, user_id, chat_id, message_id, text)
        finally:
            teacher_done.set()

    async with USER_LOCKS[user_id]:
        _queue_friend_turn(app, user_id, chat_id, teacher_done, friend_text,
                           excerpt, images, media_group_id)

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


async def handle_user_text(app: Application, update: Update, text: str,
                            images: list[Path] | None = None,
                            media_group_id: str | None = None) -> None:
    """Shared entry point for text, transcribed voice and captioned photos.
    Routes replies-to-teacher to a follow-up turn, rejects replies to anything
    else, otherwise performs the normal dual dispatch. `images` go to the
    friend only."""
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
            # The teacher never sees photos; keep them for the next friend turn.
            for path in images or []:
                stage(user_id, path)
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
                quoted = _quoted_text(replied)
            await dispatch_user_text(
                app, user_id, chat_id, message_id, text, quoted=quoted,
                images=images, media_group_id=media_group_id,
            )
            return
        await update.message.reply_text(INVALID_REPLY_MSG)
        return

    await dispatch_user_text(app, user_id, chat_id, message_id, text,
                             images=images, media_group_id=media_group_id)


async def _join_album(app: Application, user_id: int, chat_id: int,
                       path: Path, media_group_id: str) -> bool:
    """Add an uncaptioned album photo to the in-flight friend turn of the same
    album (the caption sits on just one album item) and restart that turn.
    False if no such turn is running."""
    async with USER_LOCKS[user_id]:
        pending = PENDING_FRIEND.get(user_id)
        if pending is None or pending.media_group_id != media_group_id:
            return False
        # This photo has no text of its own; the album's teacher turn already ran.
        teacher_done = asyncio.Event()
        teacher_done.set()
        _queue_friend_turn(app, user_id, chat_id, teacher_done, None,
                           pending.excerpt, [path])
        return True


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photos (and image files) go to the friend only. With a caption they are
    a normal turn and the teacher checks the caption; without one they wait
    silently for the user's next message."""
    if is_group_chat(update):
        return  # groups are teacher-only; photos are ignored there
    if not is_allowed(update):
        return  # stay silent for unauthorized users; only /start responds
    if not PHOTOS_ENABLED:
        await update.message.reply_text("Fotos sind deaktiviert.")
        return

    app = context.application
    msg = update.message
    user_id = update.effective_user.id
    try:
        path = await download_user_photo(context.bot, msg, user_id)
    except Exception:
        logger.exception("Photo download failed")
        await msg.reply_text("Konnte das Foto nicht laden.")
        return

    caption = (msg.caption or "").strip()
    if caption:
        await handle_user_text(app, update, caption, images=[path],
                               media_group_id=msg.media_group_id)
        return
    if msg.media_group_id and await _join_album(
        app, user_id, update.effective_chat.id, path, msg.media_group_id
    ):
        return
    stage(user_id, path)


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
