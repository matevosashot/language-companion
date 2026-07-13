"""/status (session overview) and /ping (reachability)."""

from telegram import Update
from telegram.ext import ContextTypes

from ..config import (
    FRIEND_DEFAULT_LEVEL,
    FRIEND_HISTORY_MAX,
    FRIEND_PROFILES,
    TEACHER_GROUP_LEVEL,
    VOICE_ENABLED,
    friend_profile,
)
from ..friend import PENDING_FRIEND
from ..sessions import active_profile_id, load_user, profile_state
from .common import _teacher_authorized, is_group_chat


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _teacher_authorized(update):
        return
    user_id = update.effective_user.id

    if is_group_chat(update):
        user = load_user(user_id, update.effective_chat.id)
        teacher = user.get("teacher")
        chosen = user.get("user_level")
        level = chosen or TEACHER_GROUP_LEVEL
        level_src = "selbst gewählt" if chosen else "Gruppen-Standard"
        await update.message.reply_text(
            f"User ID: {user_id}\n"
            f"Modus: Gruppe (nur Lehrer)\n"
            f"Dein Niveau: {level} ({level_src})\n"
            f"Freund: n/a (Gruppe)\n"
            f"Lehrer-Session: {'aktiv' if teacher else 'leer'}"
        )
        return

    user = load_user(user_id)
    pid = active_profile_id(user)
    meta = friend_profile(pid)
    st = profile_state(user, pid)
    history = st.get("history") or []
    teacher = user.get("teacher")
    level = user.get("user_level") or FRIEND_DEFAULT_LEVEL
    in_flight = user_id in PENDING_FRIEND
    if not meta.onboarding:
        setup = "kein Setup nötig"
    else:
        setup = "ja" if st.get("answers") else "nein"
    available = " · ".join(p.label for p in FRIEND_PROFILES.values()) or "(keine)"
    await update.message.reply_text(
        f"User ID: {user_id}\n"
        f"Aktiver Freund: {meta.label}\n"
        f"Verfügbare Profile: {available}\n"
        f"Eingerichtet: {setup}\n"
        f"Dein Niveau: {level}\n"
        f"Freund-Historie: {len(history)}/{FRIEND_HISTORY_MAX}\n"
        f"Lehrer-Session: {'aktiv' if teacher else 'leer'}\n"
        f"Antwort in Arbeit: {'ja' if in_flight else 'nein'}\n"
        f"Voice: {'an' if VOICE_ENABLED else 'aus'}"
    )
