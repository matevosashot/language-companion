"""Background loop that nudges users who have been silent too long."""

import asyncio
import datetime as dt
import logging

from telegram.ext import Application

from .config import IDLE_CHECK_INTERVAL, IDLE_PING_AFTER, IDLE_PROMPT
from .friend import PENDING_FRIEND, PendingFriend, friend_turn
from .sessions import iter_user_ids, load_user, now_iso, save_user

logger = logging.getLogger(__name__)


async def idle_pinger(app: Application) -> None:
    await asyncio.sleep(5)  # let things settle on startup
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
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
                pending = PendingFriend(queued=[IDLE_PROMPT], excerpt="[idle-ping]")
                PENDING_FRIEND[uid] = pending
                pending.task = asyncio.create_task(friend_turn(app, uid, chat_id))
        except Exception:
            logger.exception("idle_pinger iteration failed")
        await asyncio.sleep(IDLE_CHECK_INTERVAL)
