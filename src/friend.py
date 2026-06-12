"""Friend persona: prompt building, per-user pending-turn state, friend_turn."""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from telegram.ext import Application

from .claude_runner import run_claude
from .config import FRIEND_DEFAULT_LEVEL, FRIEND_HISTORY_MAX, FRIEND_MODEL, FRIEND_TOOLS, FRIEND_WORKDIR
from .markdown import to_telegram_html
from .sessions import load_user, now_iso, save_user

logger = logging.getLogger(__name__)

# How many recent friend message ids to remember, so replies to older friend
# messages can still be recognized.
FRIEND_MSG_ID_MAX = 100


DEFAULT_PROFILE = {
    "name": "Klaus", "age": 34, "gender": "männlich",
    "city": "Leipzig", "extras": "",
}

PROFILE_PREFIX = (
    "[PROFIL — lies das einmal still und bleib dann durchgehend in dieser Rolle]\n"
    "Name: {name}\n"
    "Alter: {age}\n"
    "Geschlecht: {gender}\n"
    "Stadt: {city}\n"
    "Besonderheiten: {extras}\n\n"
    "[NUTZER-NIVEAU] {level} (CEFR) — passe Wortschatz und Satzbau "
    "entsprechend an. Bei A1/A2 einfache Sätze und gebräuchliche Wörter; "
    "bei B1/B2 normales Gesprächsdeutsch; bei C1/C2 darfst du auch "
    "anspruchsvolle Wendungen und Dialektnuancen einbauen.\n\n"
    "Folge CLAUDE.md (lies sie still einmal) und sprich ab jetzt als diese "
    "Person mit mir. Antworte auf die folgende Nachricht:\n---\n{msg}"
)


def build_friend_first_message(profile: dict, level: str, msg: str) -> str:
    return PROFILE_PREFIX.format(
        name=profile.get("name") or DEFAULT_PROFILE["name"],
        age=profile.get("age") or DEFAULT_PROFILE["age"],
        gender=profile.get("gender") or DEFAULT_PROFILE["gender"],
        city=profile.get("city") or DEFAULT_PROFILE["city"],
        extras=profile.get("extras") or "(keine)",
        level=level or FRIEND_DEFAULT_LEVEL,
        msg=msg,
    )


@dataclass
class PendingFriend:
    task: Optional[asyncio.Task] = None
    proc_holder: dict = field(default_factory=dict)
    queued: list[str] = field(default_factory=list)
    excerpt: str = ""
    keeper_task: Optional[asyncio.Task] = None


PENDING_FRIEND: dict[int, PendingFriend] = {}
USER_LOCKS: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


async def cancel_pending_friend(user_id: int) -> None:
    pending = PENDING_FRIEND.pop(user_id, None)
    if pending is None:
        return
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


async def friend_turn(app: Application, user_id: int, chat_id: int) -> None:
    pending = PENDING_FRIEND.get(user_id)
    if pending is None:
        return
    try:
        user = load_user(user_id)
        history = user.get("friend_history") or []
        cur_sid = history[-1]["sid"] if history else None
        profile = user.get("friend_profile") or DEFAULT_PROFILE
        level = user.get("user_level") or FRIEND_DEFAULT_LEVEL

        combined = "\n---\n".join(pending.queued)
        excerpt = pending.excerpt or (pending.queued[0][:60] if pending.queued else "")

        prompt = combined if cur_sid else build_friend_first_message(profile, level, combined)

        result = await run_claude(prompt, FRIEND_WORKDIR, cur_sid, FRIEND_TOOLS, pending.proc_holder, FRIEND_MODEL)

        if result.cancelled:
            return

        if result.expired and cur_sid:
            logger.info("Friend session %s expired, restarting fresh", cur_sid)
            user["friend_history"] = []
            prompt = build_friend_first_message(profile, level, combined)
            result = await run_claude(prompt, FRIEND_WORKDIR, None, FRIEND_TOOLS, pending.proc_holder, FRIEND_MODEL)
            if result.cancelled:
                return

        if result.error or not result.text:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"(Freund-Fehler: {result.error or 'leere Antwort'})",
            )
            return

        user = load_user(user_id)
        history = user.get("friend_history") or []
        if result.session_id:
            history.append({
                "sid": result.session_id,
                "user_excerpt": excerpt,
                "ts": now_iso(),
            })
            if len(history) > FRIEND_HISTORY_MAX:
                history = history[-FRIEND_HISTORY_MAX:]
            user["friend_history"] = history
        user["chat_id"] = chat_id
        user["last_message_ts"] = now_iso()
        user["idle_ping_sent_at"] = None
        user["last_friend_reply"] = result.text
        save_user(user_id, user)

        reply_html = to_telegram_html(result.text)
        if len(reply_html) > 4000:
            reply_html = reply_html[:4000] + "\n\n… (gekürzt)"
        sent = await app.bot.send_message(
            chat_id=chat_id, text=reply_html, parse_mode="HTML",
        )

        user = load_user(user_id)
        ids = user.get("friend_message_ids") or []
        ids.append(sent.message_id)
        user["friend_message_ids"] = ids[-FRIEND_MSG_ID_MAX:]
        save_user(user_id, user)

    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("friend_turn crashed")
    finally:
        cur = PENDING_FRIEND.get(user_id)
        if cur is pending:
            PENDING_FRIEND.pop(user_id, None)
