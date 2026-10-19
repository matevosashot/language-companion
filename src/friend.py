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
from pathlib import Path
from typing import Optional

from telegram.ext import Application

from .claude_runner import run_claude
from .config import (
    FRIEND_DEFAULT_LEVEL,
    FRIEND_HISTORY_MAX,
    PHOTOS_ENABLED,
    STREAMING_ENABLED,
    friend_profile,
)
from .markdown import to_telegram_html
from .photos import OutPhoto, build_photo_server, send_outbox
from .sessions import active_profile_id, load_user, now_iso, profile_state, save_user
from .streaming import DraftStreamer

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


def _photo_marker(n: int) -> str:
    return "[Foto vom Nutzer]" if n == 1 else f"[{n} Fotos vom Nutzer]"


@dataclass
class PendingFriend:
    task: Optional[asyncio.Task] = None
    queued: list[str] = field(default_factory=list)
    excerpt: str = ""
    keeper_task: Optional[asyncio.Task] = None
    # User photos riding along with the queued texts, and the Telegram album
    # they came from (later uncaptioned album items join this turn).
    images: list[Path] = field(default_factory=list)
    media_group_id: Optional[str] = None


PENDING_FRIEND: dict[int, PendingFriend] = {}
USER_LOCKS: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


async def cancel_pending_friend(user_id: int) -> None:
    pending = PENDING_FRIEND.pop(user_id, None)
    if pending is None:
        return
    if pending.task is not None and not pending.task.done():
        pending.task.cancel()
    if pending.keeper_task is not None and not pending.keeper_task.done():
        pending.keeper_task.cancel()


async def friend_turn(app: Application, user_id: int, chat_id: int) -> None:
    pending = PENDING_FRIEND.get(user_id)
    if pending is None:
        return
    streamer: Optional[DraftStreamer] = None

    def stream_kw() -> dict:
        """Fresh live-draft streamer for one run_claude attempt."""
        nonlocal streamer
        if streamer is not None:
            streamer.abort()
        if not STREAMING_ENABLED:
            return {}
        streamer = DraftStreamer(app.bot, chat_id)
        return {"on_text": streamer.on_text, "on_tool": streamer.on_tool}

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
        if pending.images:
            combined = f"{_photo_marker(len(pending.images))}\n{combined}"
        excerpt = pending.excerpt or (pending.queued[0][:60] if pending.queued else "")

        prompt = combined if cur_sid else build_friend_first_message(meta, answers, level, combined)

        # Photos the friend picks land in `outbox` and go out after its text.
        outbox: list[OutPhoto] = []
        photo_kw = {"images": pending.images,
                    "thinking": meta.thinking, "effort": meta.effort}
        if PHOTOS_ENABLED:
            server, names = build_photo_server(outbox)
            photo_kw.update(mcp_servers={"fotos": server}, extra_tools=names)

        result = await run_claude(prompt, meta.workdir, cur_sid, meta.tools,
                                  model=meta.model, **photo_kw, **stream_kw())

        if result.cancelled:
            return

        expired_restart = False
        if result.expired and cur_sid:
            logger.info("Friend session %s (profile %s) expired, restarting fresh", cur_sid, pid)
            expired_restart = True
            prompt = build_friend_first_message(meta, answers, level, combined)
            outbox.clear()
            result = await run_claude(prompt, meta.workdir, None, meta.tools,
                                      model=meta.model, **photo_kw, **stream_kw())
            if result.cancelled:
                return

        # Text written before tool calls already went out as its own messages;
        # don't repeat it if the turn's last text was one of those.
        seg_ids = await streamer.finish() if streamer else []
        seg_texts = streamer.sent_texts if streamer else []
        final = (result.text or "").strip()
        if seg_texts and final == seg_texts[-1]:
            final = ""

        if result.error or not (final or outbox or seg_ids):
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
        last_text = final or (seg_texts[-1] if seg_texts else "")
        if last_text:
            st["last_reply"] = last_text
        save_user(user_id, user)

        sent_ids = list(seg_ids)
        if final:
            # Sending the real message also clears the live draft.
            reply_html = to_telegram_html(final)
            if len(reply_html) > 4000:
                reply_html = reply_html[:4000] + "\n\n… (gekürzt)"
            sent = await app.bot.send_message(
                chat_id=chat_id, text=reply_html, parse_mode="HTML",
            )
            sent_ids.append(sent.message_id)
        sent_ids += await send_outbox(app.bot, chat_id, outbox)

        user = load_user(user_id)
        st = profile_state(user, pid)
        ids = st.get("message_ids") or []
        ids.extend(sent_ids)
        st["message_ids"] = ids[-FRIEND_MSG_ID_MAX:]
        save_user(user_id, user)

    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("friend_turn crashed")
    finally:
        if streamer is not None:
            streamer.abort()  # no-op once finished
        cur = PENDING_FRIEND.get(user_id)
        if cur is pending:
            PENDING_FRIEND.pop(user_id, None)
