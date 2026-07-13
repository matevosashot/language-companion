"""/reload — re-read CLAUDE.md inside an existing session.

CLAUDE.md is baked into the system prompt at session start. To pick up changes
without losing conversation history, we send one synthetic turn that tells the
persona to Read CLAUDE.md again as a tool call. The freshly-read file lands in
the conversation context and supersedes the baked-in copy from now on. The new
session_id replaces the latest one in place — no new /rewind checkpoint added."""

from telegram import Update
from telegram.ext import ContextTypes

from ..claude_runner import run_claude
from ..config import TEACHER_MODEL, TEACHER_TOOLS, TEACHER_WORKDIR, friend_profile
from ..friend import cancel_pending_friend
from ..sessions import active_profile_id, load_user, now_iso, profile_state, save_user
from .common import is_allowed, is_group_chat

RELOAD_PROMPT = (
    "[SYSTEM] Lies bitte CLAUDE.md noch einmal vollständig mit dem "
    "Read-Tool — die Datei wurde gerade aktualisiert. Befolge die neuen "
    "Anweisungen ab jetzt durchgehend. Antworte zur Bestätigung nur mit "
    'dem einzigen Wort "ok", nichts weiter.'
)


async def reload_friend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        return  # friend persona is disabled in groups
    if not is_allowed(update):
        return
    user_id = update.effective_user.id
    user = load_user(user_id)
    pid = active_profile_id(user)
    meta = friend_profile(pid)
    history = profile_state(user, pid).get("history") or []
    if not history:
        await update.message.reply_text(
            "Noch keine Freund-Session aktiv — die nächste Nachricht lädt "
            "CLAUDE.md ohnehin frisch."
        )
        return

    await cancel_pending_friend(user_id)
    cur_sid = history[-1]["sid"]
    status = await update.message.reply_text("🔄 Freund liest CLAUDE.md neu…")
    result = await run_claude(RELOAD_PROMPT, meta.workdir, cur_sid, meta.tools, model=meta.model)
    if result.cancelled or result.error or not result.session_id:
        await status.edit_text(f"Reload fehlgeschlagen: {result.error or 'unbekannt'}")
        return

    user = load_user(user_id)
    st = profile_state(user, pid)
    history = st.get("history") or []
    if history:
        history[-1] = {**history[-1], "sid": result.session_id, "ts": now_iso()}
        st["history"] = history
        save_user(user_id, user)
    await status.edit_text("✅ Freund hat CLAUDE.md neu gelesen.")


async def reload_teacher_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        return  # /reload* are private-only; use /new_teacher in groups
    if not is_allowed(update):
        return
    user_id = update.effective_user.id
    user = load_user(user_id)
    sid = user.get("teacher")
    if not sid:
        await update.message.reply_text(
            "Noch keine Lehrer-Session aktiv — die nächste Korrektur lädt "
            "CLAUDE.md ohnehin frisch."
        )
        return

    status = await update.message.reply_text("🔄 Lehrer liest CLAUDE.md neu…")
    result = await run_claude(RELOAD_PROMPT, TEACHER_WORKDIR, sid, TEACHER_TOOLS, model=TEACHER_MODEL)
    if result.cancelled or result.error or not result.session_id:
        await status.edit_text(f"Reload fehlgeschlagen: {result.error or 'unbekannt'}")
        return

    user = load_user(user_id)
    user["teacher"] = result.session_id
    save_user(user_id, user)
    await status.edit_text("✅ Lehrer hat CLAUDE.md neu gelesen.")


async def reload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reload both personas. Sequential to keep status messages readable."""
    await reload_friend_cmd(update, context)
    await reload_teacher_cmd(update, context)
