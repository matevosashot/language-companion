"""Live streaming of the friend's reply through Telegram message drafts
(sendMessageDraft, private chats only).

A draft is an ephemeral, animated preview; the persistent message is still
sent with sendMessage once the reply is final (which also clears the draft).
Text the model writes before a tool call ("Moment, ich schau kurz…") is a
finished *segment* and goes out as its own message; the answer that follows
streams in a fresh draft."""

import asyncio
import logging
import random
import time
from typing import Optional

from telegram import Bot
from telegram.error import BadRequest, RetryAfter, TelegramError

from .markdown import to_telegram_html

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 0.4   # min gap between draft updates
# Drafts vanish after ~30 s; re-send an unchanged one before that.
KEEPALIVE_S = 20
DRAFT_MAX = 4096
MESSAGE_MAX = 4000


def _new_draft_id() -> int:
    return random.randint(1, 2**31 - 1)


def _retry_seconds(e: RetryAfter) -> float:
    ra = e.retry_after
    return ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)


class DraftStreamer:
    """Feed it with `on_text` / `on_tool` (sync, from the SDK read loop); a
    single background task does all Telegram I/O, so messages stay in order."""

    def __init__(self, bot: Bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.sent_ids: list[int] = []
        self.sent_texts: list[str] = []
        self._buf = ""
        self._dirty = False
        self._segments: list[str] = []
        self._draft_id = _new_draft_id()
        self._drafts_ok = True
        self._last_draft_at = 0.0
        self._stopping = False
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # -- hooks (sync) ------------------------------------------------------

    def on_text(self, chunk: str) -> None:
        self._buf += chunk
        self._dirty = True
        self._kick()

    def on_tool(self) -> None:
        text = self._buf.strip()
        self._buf = ""
        self._dirty = False
        if text:
            self._segments.append(text)
            self._draft_id = _new_draft_id()
            self._kick()

    # -- lifecycle ---------------------------------------------------------

    async def finish(self) -> list[int]:
        """Stop streaming and send any finished segments still queued. The
        caller sends the final text itself. Returns the segment message ids."""
        self._stopping = True
        if self._task is None:
            await self._send_segments()
        else:
            self._wake.set()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        return self.sent_ids

    def abort(self) -> None:
        """Drop everything (the turn was cancelled). A visible draft fades out
        on its own or is replaced by the next turn's draft."""
        self._stopping = True
        self._segments.clear()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # -- internals ---------------------------------------------------------

    def _kick(self) -> None:
        if self._stopping:
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        self._wake.set()

    async def _run(self) -> None:
        try:
            while True:
                await self._send_segments()
                if self._stopping:
                    return
                stale = time.monotonic() - self._last_draft_at > KEEPALIVE_S
                if self._drafts_ok and self._buf.strip() and (self._dirty or stale):
                    self._dirty = False
                    await self._send_draft()
                    await asyncio.sleep(FLUSH_INTERVAL_S)
                    continue
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=KEEPALIVE_S)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Draft streamer crashed")

    async def _send_draft(self) -> None:
        self._last_draft_at = time.monotonic()
        text = self._buf
        try:
            await self.bot.send_message_draft(
                chat_id=self.chat_id, draft_id=self._draft_id,
                text=to_telegram_html(text)[:DRAFT_MAX], parse_mode="HTML",
            )
            return
        except RetryAfter as e:
            await asyncio.sleep(_retry_seconds(e))
            self._dirty = True
            return
        except BadRequest:
            pass  # half-written markdown that renders to invalid HTML
        except TelegramError as e:
            logger.warning("sendMessageDraft failed, streaming off for this turn: %s", e)
            self._drafts_ok = False
            return
        try:
            await self.bot.send_message_draft(
                chat_id=self.chat_id, draft_id=self._draft_id, text=text[:DRAFT_MAX],
            )
        except TelegramError as e:
            logger.warning("sendMessageDraft (plain) failed, streaming off: %s", e)
            self._drafts_ok = False

    async def _send_segments(self) -> None:
        while self._segments:
            text = self._segments.pop(0)
            try:
                try:
                    sent = await self.bot.send_message(
                        chat_id=self.chat_id, parse_mode="HTML",
                        text=to_telegram_html(text)[:MESSAGE_MAX],
                    )
                except BadRequest:
                    sent = await self.bot.send_message(
                        chat_id=self.chat_id, text=text[:MESSAGE_MAX],
                    )
            except TelegramError:
                logger.exception("Sending friend segment failed")
                continue
            self.sent_ids.append(sent.message_id)
            self.sent_texts.append(text)
