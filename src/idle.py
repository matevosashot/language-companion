"""Background loop that nudges users who have been silent too long."""

import asyncio
import datetime as dt
import logging

from telegram.ext import Application

from .claude_runner import compact_session, get_context
from .config import (
    FRIEND_COMPACT_AT,
    IDLE_CHECK_INTERVAL,
    IDLE_PING_AFTER,
    IDLE_PROMPT,
    friend_profile,
)
from .friend import PENDING_FRIEND, PendingFriend, friend_turn
from .sessions import (
    active_profile_id,
    iter_user_ids,
    load_user,
    now_iso,
    profile_state,
    save_user,
)

logger = logging.getLogger(__name__)


async def _compact_idle_friend(user: dict) -> None:
    """Shrink the active profile's session before the re-engagement ping, so
    the ping itself is composed on the smaller context. Best effort — the ping
    must still go out if this fails."""
    if FRIEND_COMPACT_AT <= 0:
        return
    pid = active_profile_id(user)
    meta = friend_profile(pid)
    history = profile_state(user, pid).get("history") or []
    if not history:
        return
    sid = history[-1]["sid"]

    usage = await get_context(meta.workdir, sid, meta.tools, meta.model)
    if usage is None:
        return
    tokens = usage["totalTokens"]
    if tokens < FRIEND_COMPACT_AT:
        return

    logger.info("Friend session %s (profile %s) at %d tokens (limit %d) — compacting",
                sid, pid, tokens, FRIEND_COMPACT_AT)
    result = await compact_session(meta.workdir, sid, meta.tools, meta.model)
    if result.error:
        logger.warning("Friend compaction failed: %s", result.error)
    else:
        logger.info("Friend session %s compacted (was %d tokens)", sid, tokens)


async def idle_pinger(app: Application) -> None:
    await asyncio.sleep(5)  # let things settle on startup
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            # iter_user_ids() only enumerates private-chat files; group teacher
            # state lives in groups/ and is intentionally never idle-pinged
            # (no friend persona runs in groups).
            for uid in iter_user_ids():
                user = load_user(uid)
                last = user.get("last_message_ts")
                chat_id = user.get("chat_id")
                if not last or not chat_id:
                    continue
                try:
                    last_dt = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
                except ValueError:
                    continue
                idle_s = (now - last_dt).total_seconds()
                if idle_s < IDLE_PING_AFTER:
                    continue
                pinged = user.get("idle_ping_sent_at")
                if pinged:
                    try:
                        pinged_dt = dt.datetime.fromisoformat(pinged.replace("Z", "+00:00"))
                        if pinged_dt > last_dt:
                            continue
                    except ValueError:
                        pass

                logger.info("Idle ping for user %s (idle %ss)", uid, int(idle_s))
                user["idle_ping_sent_at"] = now_iso()
                save_user(uid, user)

                if uid in PENDING_FRIEND:
                    continue

                try:
                    await _compact_idle_friend(user)
                except Exception:
                    logger.exception("Idle compaction failed for user %s", uid)
                pending = PendingFriend(queued=[IDLE_PROMPT], excerpt="[idle-ping]")
                PENDING_FRIEND[uid] = pending
                pending.task = asyncio.create_task(friend_turn(app, uid, chat_id))
        except Exception:
            logger.exception("idle_pinger iteration failed")
        await asyncio.sleep(IDLE_CHECK_INTERVAL)
