"""Teacher persona: stateless per-turn grammar / style corrector."""

import logging
from typing import Optional

from telegram import ReactionTypeEmoji
from telegram.ext import Application

from .claude_runner import run_claude
from .config import (
    TEACHER_DEFAULT_LEVEL,
    TEACHER_MODEL,
    TEACHER_OK_MARKER,
    TEACHER_OK_REACTION,
    TEACHER_TOOLS,
    TEACHER_WORKDIR,
)
from .markdown import to_telegram_html
from .sessions import load_user, save_user

logger = logging.getLogger(__name__)

FRIEND_KONTEXT_LABEL = "letzte Nachricht von Klaus, dem Freund"
GROUP_KONTEXT_LABEL = "vorherige Nachricht im Chat"


def build_teacher_prompt(context_text: Optional[str], user_message: str,
                          level: str, kontext_label: str = FRIEND_KONTEXT_LABEL) -> str:
    head = f"[NUTZER-NIVEAU] {level}\n\n"
    if context_text:
        return (
            f"{head}"
            f"[KONTEXT — {kontext_label}]\n"
            f"{context_text}\n\n"
            "[ANTWORT DES NUTZERS — bitte prüfen]\n"
            f"{user_message}"
        )
    return (
        f"{head}"
        "[ANTWORT DES NUTZERS — bitte prüfen]\n"
        f"{user_message}"
    )


def build_followup_prompt(user_message: str) -> str:
    return (
        "[NACHFRAGE — Folgefrage des Nutzers zur letzten Korrektur]\n"
        f"{user_message}"
    )


async def _send_teacher_reply(app: Application, user_id: int, chat_id: int,
                                message_id: int, body: str,
                                state_chat_id: int | None = None) -> None:
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
    user = load_user(user_id, state_chat_id)
    user["last_teacher_message_id"] = sent.message_id
    save_user(user_id, user, state_chat_id)


async def teacher_turn(app: Application, user_id: int, chat_id: int,
                        message_id: int, text: str,
                        state_chat_id: int | None = None,
                        context_text: str | None = None,
                        level: str | None = None,
                        kontext_label: str = FRIEND_KONTEXT_LABEL) -> None:
    user = load_user(user_id, state_chat_id)
    sid = user.get("teacher")
    # Private: the friend's last reply is the context. Group: the explicit
    # context_text (the human message being replied to), if any.
    if context_text is None and state_chat_id is None:
        context_text = user.get("last_friend_reply")
    if level is None:
        level = user.get("user_level") or TEACHER_DEFAULT_LEVEL

    prompt = build_teacher_prompt(context_text, text, level, kontext_label)

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
        user = load_user(user_id, state_chat_id)
        user["teacher"] = result.session_id
        save_user(user_id, user, state_chat_id)

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

    await _send_teacher_reply(app, user_id, chat_id, message_id, body, state_chat_id)


async def teacher_followup_turn(app: Application, user_id: int, chat_id: int,
                                  message_id: int, text: str,
                                  state_chat_id: int | None = None) -> None:
    user = load_user(user_id, state_chat_id)
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
        user = load_user(user_id, state_chat_id)
        user["teacher"] = result.session_id
        save_user(user_id, user, state_chat_id)

    await _send_teacher_reply(app, user_id, chat_id, message_id, result.text.strip(), state_chat_id)
