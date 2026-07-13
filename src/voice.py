"""Voice transcription via mlx-whisper (Apple Silicon)."""

import asyncio
import html
import os
import tempfile

from telegram import Update
from telegram.ext import ContextTypes

from .config import VOICE_ENABLED, VOICE_MODEL


def _transcribe_sync(path: str) -> str:
    import mlx_whisper
    r = mlx_whisper.transcribe(path, path_or_hf_repo=VOICE_MODEL, verbose=False)
    return (r.get("text") or "").strip()


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .handlers import handle_user_text, is_allowed, is_group_chat

    if is_group_chat(update):
        return  # v1: voice notes are ignored in groups (text-only)
    if not is_allowed(update):
        return  # stay silent for unauthorized users; only /start responds
    if not VOICE_ENABLED:
        await update.message.reply_text(
            "Sprachnachrichten werden nicht unterstützt (mlx-whisper fehlt)."
        )
        return

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        path = tmp.name
    await file.download_to_drive(path)
    try:
        transcript = await asyncio.to_thread(_transcribe_sync, path)
        if not transcript:
            await update.message.reply_text("Konnte die Sprachnachricht nicht transkribieren.")
            return
        await update.message.reply_text(
            f"📝 <i>{html.escape(transcript)}</i>", parse_mode="HTML",
        )
        await handle_user_text(context.application, update, transcript)
    finally:
        if os.path.exists(path):
            os.remove(path)
