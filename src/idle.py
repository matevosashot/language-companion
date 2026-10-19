"""Background loop that nudges users who have been silent too long."""

import asyncio
import datetime as dt
import logging

from telegram.ext import Application

from .claude_runner import compact_session, get_context
from .config import (
    FRIEND_COMPACT_AT,
    IDLE_CHECK_INTERVAL,
    IDLE_PING_SCHEDULE,
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


def _parse_ts(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def ping_due(user: dict, now: dt.datetime) -> bool:
    """Whether the next idle ping is due. The n-th ping since the user's last
    message waits IDLE_PING_SCHEDULE[n] after the previous ping (the first one
    after the user's message); past the end, the last gap repeats."""
    last = _parse_ts(user.get("last_message_ts"))
    if last is None:
        return False
    count = int(user.get("idle_pings") or 0)
    pinged = _parse_ts(user.get("idle_ping_sent_at"))
    anchor = pinged if count and pinged and pinged > last else last
    if anchor is last:
        count = 0
    gap = IDLE_PING_SCHEDULE[min(count, len(IDLE_PING_SCHEDULE) - 1)]
    return (now - anchor).total_seconds() >= gap


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
                chat_id = user.get("chat_id")
                if not chat_id or not ping_due(user, now):
                    continue

                last = _parse_ts(user.get("last_message_ts")) or now
                count = int(user.get("idle_pings") or 0) + 1
                logger.info("Idle ping #%d for user %s (silent %.1f days)",
                            count, uid, (now - last).total_seconds() / 86400)
                user["idle_ping_sent_at"] = now_iso()
                user["idle_pings"] = count
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
