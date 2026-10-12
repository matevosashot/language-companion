"""/status (session overview), /context (context-window usage) and /ping."""

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from ..claude_runner import get_context
from ..config import (
    FRIEND_COMPACT_AT,
    FRIEND_DEFAULT_LEVEL,
    FRIEND_HISTORY_MAX,
    FRIEND_PROFILES,
    TEACHER_COMPACT_AT,
    TEACHER_GROUP_LEVEL,
    TEACHER_MODEL,
    TEACHER_TOOLS,
    TEACHER_WORKDIR,
    VOICE_ENABLED,
    friend_profile,
)
from ..friend import PENDING_FRIEND
from ..sessions import active_profile_id, load_user, profile_state
from .common import _teacher_authorized, is_group_chat

# Redundant with the percentage, and it dominates the list on a fresh session.
_SKIP_CATEGORIES = {"Free space"}


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


def _tok(n: int) -> str:
    """1234 -> '1.2k'."""
    return f"{n / 1000:.1f}k"


def _format_context(label: str, usage: dict | None, compact_at: int) -> str:
    if usage is None:
        return f"<b>{label}</b>\n  — keine Session"

    total, max_t = usage["totalTokens"], usage["maxTokens"]
    lines = [
        f"<b>{label}</b>",
        f"  {_tok(total)} / {_tok(max_t)} ({usage['percentage']:.0f} %) · {usage['model']}",
    ]
    for cat in usage.get("categories") or []:
        if cat["name"] in _SKIP_CATEGORIES or not cat["tokens"]:
            continue
        lines.append(f"  · {cat['name']}: {_tok(cat['tokens'])}")

    if compact_at > 0:
        note = f"  Compact ab {_tok(compact_at)}"
        if compact_at > max_t:
            # Surfaced because it is otherwise invisible: the limit sits above
            # the window, so the automatic /compact can never trigger.
            note += " — über dem Limit, wird nie ausgelöst"
        lines.append(note)
    return "\n".join(lines)


async def context_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report Claude Code context usage for the sessions in play."""
    if not _teacher_authorized(update):
        return
    user_id = update.effective_user.id
    group = is_group_chat(update)
    state_chat_id = update.effective_chat.id if group else None
    user = load_user(user_id, state_chat_id)

    status = await update.message.reply_text("📊 Lese Kontext-Auslastung…")

    jobs = []
    if not group:  # no friend persona runs in groups
        pid = active_profile_id(user)
        meta = friend_profile(pid)
        history = profile_state(user, pid).get("history") or []
        sid = history[-1]["sid"] if history else None
        jobs.append((
            f"Freund ({meta.label})",
            get_context(meta.workdir, sid, meta.tools, meta.model),
            FRIEND_COMPACT_AT,
        ))
    jobs.append((
        "Lehrer",
        get_context(TEACHER_WORKDIR, user.get("teacher"), TEACHER_TOOLS, TEACHER_MODEL),
        TEACHER_COMPACT_AT,
    ))

    usages = await asyncio.gather(*(job[1] for job in jobs))
    blocks = [
        _format_context(label, usage, compact_at)
        for (label, _, compact_at), usage in zip(jobs, usages)
    ]
    await status.edit_text("\n\n".join(blocks), parse_mode="HTML")
