"""Teacher persona: stateless per-turn grammar / style corrector."""

import logging
from typing import Optional

from telegram import ReactionTypeEmoji
from telegram.ext import Application

from .claude_runner import run_claude
from .config import TEACHER_MODEL, TEACHER_OK_MARKER, TEACHER_OK_REACTION, TEACHER_TOOLS, TEACHER_WORKDIR
from .markdown import to_telegram_html
from .sessions import load_user, save_user

logger = logging.getLogger(__name__)


def build_teacher_prompt(friend_last_reply: Optional[str], user_message: str) -> str:
    if friend_last_reply:
        return (
            "[KONTEXT — letzte Nachricht von Klaus, dem Freund]\n"
            f"{friend_last_reply}\n\n"
            "[ANTWORT DES NUTZERS — bitte prüfen]\n"
            f"{user_message}"
        )
    return (
        "[ANTWORT DES NUTZERS — bitte prüfen]\n"
        f"{user_message}"
    )


def build_followup_prompt(user_message: str) -> str:
    return (
        "[NACHFRAGE — Folgefrage des Nutzers zur letzten Korrektur]\n"
        f"{user_message}"
    )


async def _send_teacher_reply(app: Application, user_id: int, chat_id: int,
                                message_id: int, body: str) -> None:
    reply_html = to_telegram_html(body)
    if len(reply_html) > 4000:
        reply_html = reply_html[:4000] + "\n\n… (gekürzt)"
    try:
        sent = await app.bot.send_message(
            chat_id=chat_id, text=reply_html, parse_mode="HTML",
            reply_to_message_id=message_id,
        )
    except Exception:
        logger.exception("Teacher reply failed")
        return
    user = load_user(user_id)
    user["last_teacher_message_id"] = sent.message_id
    save_user(user_id, user)


async def teacher_turn(app: Application, user_id: int, chat_id: int,
                        message_id: int, text: str) -> None:
    user = load_user(user_id)
    sid = user.get("teacher")
    friend_last_reply = user.get("last_friend_reply")

    prompt = build_teacher_prompt(friend_last_reply, text)

    result = await run_claude(prompt, TEACHER_WORKDIR, sid, TEACHER_TOOLS, model=TEACHER_MODEL)
    if result.expired and sid:
        result = await run_claude(prompt, TEACHER_WORKDIR, None, TEACHER_TOOLS, model=TEACHER_MODEL)

    if result.cancelled:
        return
    if result.error:
        logger.warning("Teacher error: %s", result.error)
        return
    if not result.text:
        return

    if result.session_id:
        user = load_user(user_id)
        user["teacher"] = result.session_id
        save_user(user_id, user)

    body = result.text.strip()
    first_line = body.splitlines()[0].strip() if body else ""
    if first_line == TEACHER_OK_MARKER:
        try:
            await app.bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id,
                reaction=[ReactionTypeEmoji(TEACHER_OK_REACTION)],
            )
        except Exception:
            logger.exception("Failed to set reaction")
        return

    await _send_teacher_reply(app, user_id, chat_id, message_id, body)


async def teacher_followup_turn(app: Application, user_id: int, chat_id: int,
                                  message_id: int, text: str) -> None:
    user = load_user(user_id)
    sid = user.get("teacher")

    prompt = build_followup_prompt(text)
    result = await run_claude(prompt, TEACHER_WORKDIR, sid, TEACHER_TOOLS, model=TEACHER_MODEL)
    if result.expired and sid:
        result = await run_claude(prompt, TEACHER_WORKDIR, None, TEACHER_TOOLS, model=TEACHER_MODEL)

    if result.cancelled:
        return
    if result.error:
        logger.warning("Teacher follow-up error: %s", result.error)
        return
    if not result.text:
        return

    if result.session_id:
        user = load_user(user_id)
        user["teacher"] = result.session_id
        save_user(user_id, user)

    await _send_teacher_reply(app, user_id, chat_id, message_id, result.text.strip())
