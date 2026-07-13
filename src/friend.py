"""Friend persona: prompt building, per-user pending-turn state, friend_turn.

The active friend is one of several selectable *profiles* (see config.py). Each
profile has its own workdir/CLAUDE.md and its own Claude session, stored under
the user's `friend_profiles[<id>]`. friend_turn resolves the active profile and
operates entirely on that profile's slice of session state.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from telegram.ext import Application

from .claude_runner import run_claude
from .config import FRIEND_DEFAULT_LEVEL, FRIEND_HISTORY_MAX, friend_profile
from .markdown import to_telegram_html
from .sessions import active_profile_id, load_user, now_iso, profile_state, save_user

logger = logging.getLogger(__name__)

# How many recent friend message ids to remember, so replies to older friend
# messages can still be recognized.
FRIEND_MSG_ID_MAX = 100


LEVEL_GUIDANCE = (
    "[NUTZER-NIVEAU] {level} (CEFR) — passe Wortschatz und Satzbau "
    "entsprechend an. Bei A1/A2 einfache Sätze und gebräuchliche Wörter; "
    "bei B1/B2 normales Gesprächsdeutsch; bei C1/C2 darfst du auch "
    "anspruchsvolle Wendungen und Dialektnuancen einbauen."
)


def _profil_block(profile_meta, answers: dict | None) -> str:
    """Render the onboarding answers as a [PROFIL] block the CLAUDE.md expects.
    Uses each question's `label` for the line key. Empty if no answers."""
    if not answers:
        return ""
    labels = {q["id"]: (q.get("label") or q["id"])
              for q in (profile_meta.onboarding or []) if "id" in q}
    lines = []
    for key, val in answers.items():
        if val in (None, ""):
            continue
        lines.append(f"{labels.get(key, key)}: {val}")
    if not lines:
        return ""
    return (
        "[PROFIL — lies das einmal still und bleib dann durchgehend in dieser Rolle]\n"
        + "\n".join(lines)
        + "\n\n"
    )


def build_friend_first_message(profile_meta, answers: dict | None,
                                level: str, msg: str) -> str:
    """First message of a fresh session: profile block (if any) + level guidance
    + the instruction to follow CLAUDE.md, then the user's message."""
    return (
        _profil_block(profile_meta, answers)
        + LEVEL_GUIDANCE.format(level=level or FRIEND_DEFAULT_LEVEL)
        + "\n\nFolge CLAUDE.md (lies sie still einmal) und sprich ab jetzt als "
          "diese Person mit mir. Antworte auf die folgende Nachricht:\n---\n"
        + msg
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
        pid = active_profile_id(user)
        meta = friend_profile(pid)
        st = profile_state(user, pid)
        history = st.get("history") or []
        cur_sid = history[-1]["sid"] if history else None
        answers = st.get("answers")
        level = user.get("user_level") or meta.level or FRIEND_DEFAULT_LEVEL

        combined = "\n---\n".join(pending.queued)
        excerpt = pending.excerpt or (pending.queued[0][:60] if pending.queued else "")

        prompt = combined if cur_sid else build_friend_first_message(meta, answers, level, combined)

        result = await run_claude(prompt, meta.workdir, cur_sid, meta.tools, pending.proc_holder, meta.model)

        if result.cancelled:
            return

        expired_restart = False
        if result.expired and cur_sid:
            logger.info("Friend session %s (profile %s) expired, restarting fresh", cur_sid, pid)
            expired_restart = True
            prompt = build_friend_first_message(meta, answers, level, combined)
            result = await run_claude(prompt, meta.workdir, None, meta.tools, pending.proc_holder, meta.model)
            if result.cancelled:
                return

        if result.error or not result.text:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"(Freund-Fehler: {result.error or 'leere Antwort'})",
            )
            return

        user = load_user(user_id)
        st = profile_state(user, pid)
        history = [] if expired_restart else (st.get("history") or [])
        if result.session_id:
            history.append({
                "sid": result.session_id,
                "user_excerpt": excerpt,
                "ts": now_iso(),
            })
            if len(history) > FRIEND_HISTORY_MAX:
                history = history[-FRIEND_HISTORY_MAX:]
            st["history"] = history
        user["chat_id"] = chat_id
        user["last_message_ts"] = now_iso()
        user["idle_ping_sent_at"] = None
        st["last_reply"] = result.text
        save_user(user_id, user)

        reply_html = to_telegram_html(result.text)
        if len(reply_html) > 4000:
            reply_html = reply_html[:4000] + "\n\n… (gekürzt)"
        sent = await app.bot.send_message(
            chat_id=chat_id, text=reply_html, parse_mode="HTML",
        )

        user = load_user(user_id)
        st = profile_state(user, pid)
        ids = st.get("message_ids") or []
        ids.append(sent.message_id)
        st["message_ids"] = ids[-FRIEND_MSG_ID_MAX:]
        save_user(user_id, user)

    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("friend_turn crashed")
    finally:
        cur = PENDING_FRIEND.get(user_id)
        if cur is pending:
            PENDING_FRIEND.pop(user_id, None)
