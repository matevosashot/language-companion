"""Friend-session controls: /profile (pick persona), /reset, /rewind.
All private-only — the friend persona never runs in groups."""

import datetime as dt

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..config import FRIEND_PROFILES, friend_profile
from ..friend import cancel_pending_friend
from ..sessions import active_profile_id, load_user, profile_state, save_user
from .common import is_allowed, is_group_chat


# ============================================================
# /profile — pick / switch the friend persona
# ============================================================

def _profile_needs_setup(user: dict, pid: str) -> bool:
    """True if the profile defines onboarding questions but has no saved answers."""
    meta = friend_profile(pid)
    if not meta.onboarding:
        return False
    return not (profile_state(user, pid).get("answers"))


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        return  # friend is private-only
    if not is_allowed(update):
        return
    user = load_user(update.effective_user.id)
    active = active_profile_id(user)
    rows = []
    for pid, meta in FRIEND_PROFILES.items():
        mark = "✓ " if pid == active else ""
        rows.append([InlineKeyboardButton(f"{mark}{meta.label}", callback_data=f"prof:{pid}")])
    await update.message.reply_text(
        "Mit wem willst du sprechen?",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if is_group_chat(update) or not is_allowed(update):
        return
    if not q.data or not q.data.startswith("prof:"):
        return
    pid = q.data.split(":", 1)[1]
    if pid not in FRIEND_PROFILES:
        await q.edit_message_text("Dieses Profil gibt es nicht mehr.")
        return
    user_id = q.from_user.id
    await cancel_pending_friend(user_id)
    user = load_user(user_id)
    user["active_friend_profile"] = pid
    save_user(user_id, user)
    meta = friend_profile(pid)

    if _profile_needs_setup(user, pid):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ Jetzt einrichten", callback_data="onbsetup"),
        ]])
        await q.edit_message_text(
            f"Du sprichst jetzt mit {meta.label}.\n"
            "Dieser Freund braucht noch ein kurzes Setup — /setup oder Knopf unten.",
            reply_markup=kb,
        )
        return
    await q.edit_message_text(
        f"Du sprichst jetzt mit {meta.label}. Schreib was, dann geht's los."
    )


# ============================================================
# /reset — reset the active friend profile's session
# ============================================================

RESET_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("Ja, zurücksetzen", callback_data="rstf:yes"),
    InlineKeyboardButton("Abbrechen",        callback_data="rstf:no"),
]])


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        return  # friend is private-only
    if not is_allowed(update):
        return
    user = load_user(update.effective_user.id)
    meta = friend_profile(active_profile_id(user))
    await update.message.reply_text(
        f"Das setzt deinen aktuellen Freund ({meta.label}) zurück. "
        "Der Gesprächsverlauf geht verloren, das Profil bleibt.\n\n"
        "Wirklich zurücksetzen?",
        reply_markup=RESET_KB,
    )


async def reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if is_group_chat(update) or not is_allowed(update):
        return
    if not q.data or not q.data.startswith("rstf:"):
        return
    if q.data == "rstf:no":
        await q.edit_message_text("Abgebrochen. Alles bleibt wie es war.")
        return
    if q.data != "rstf:yes":
        return
    user_id = q.from_user.id
    await cancel_pending_friend(user_id)
    user = load_user(user_id)
    pid = active_profile_id(user)
    st = profile_state(user, pid)
    st["history"] = []
    st["last_reply"] = None
    st["message_ids"] = []
    save_user(user_id, user)
    meta = friend_profile(pid)
    await q.edit_message_text(
        f"⏮ {meta.label} fängt beim nächsten Mal frisch an (gleiches Profil)."
    )


# ============================================================
# /rewind
# ============================================================

def _human_delta(ts_iso: str) -> str:
    if not ts_iso:
        return ""
    try:
        t = dt.datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    now = dt.datetime.now(dt.timezone.utc)
    secs = max(0, int((now - t).total_seconds()))
    if secs < 60: return f"vor {secs}s"
    if secs < 3600: return f"vor {secs // 60}min"
    if secs < 86400: return f"vor {secs // 3600}h"
    return f"vor {secs // 86400}d"


async def rewind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        return  # rewind targets the friend, which is disabled in groups
    if not is_allowed(update):
        return
    user_id = update.effective_user.id
    args = context.args or []

    user = load_user(user_id)
    history = profile_state(user).get("history") or []

    if args and args[0].lower() == "list":
        if not history:
            await update.message.reply_text("Nichts zum Zurückspulen.")
            return
        lines = [f"Aktueller Stand ({len(history)} Schritte möglich):"]
        buttons = []
        # newest first → index 1 = most recent turn (= /rewind 1)
        for i, entry in enumerate(reversed(history), start=1):
            excerpt = (entry.get("user_excerpt") or "")[:60]
            delta = _human_delta(entry.get("ts", ""))
            lines.append(f' {i} ({delta})  "{excerpt}"')
            buttons.append(InlineKeyboardButton(f"⏪ {i}", callback_data=f"rw:{i}"))
        rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        await update.message.reply_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    try:
        n = int(args[0]) if args else 1
    except ValueError:
        await update.message.reply_text(
            "Benutzung: /rewind, /rewind N, oder /rewind list"
        )
        return

    await _perform_rewind(context, user_id, update.effective_chat.id, n)


async def rewind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if is_group_chat(update):
        return
    if not is_allowed(update):
        return
    if not q.data or not q.data.startswith("rw:"):
        return
    try:
        n = int(q.data.split(":", 1)[1])
    except ValueError:
        return
    await _perform_rewind(context, q.from_user.id, q.message.chat_id, n)


def _friend_display_name(user: dict) -> str:
    """Best display name for the active friend: the onboarded name if set,
    otherwise the profile's label."""
    answers = profile_state(user).get("answers") or {}
    if answers.get("name"):
        return answers["name"]
    return friend_profile(active_profile_id(user)).label


async def _perform_rewind(context: ContextTypes.DEFAULT_TYPE,
                           user_id: int, chat_id: int, n: int) -> None:
    user = load_user(user_id)
    history = profile_state(user).get("history") or []

    async def reply(text: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=text)

    if n < 1:
        await reply("N muss ≥ 1 sein.")
        return
    if not history:
        await reply("Nichts zum Zurückspulen.")
        return
    if n > len(history):
        await reply(
            f"Du hast nur {len(history)} Schritte — /rewind {len(history)} "
            "ist das Maximum."
        )
        return

    await cancel_pending_friend(user_id)

    user = load_user(user_id)
    st = profile_state(user)
    history = st.get("history") or []
    st["history"] = history[:-n] if n < len(history) else []
    st["last_reply"] = None
    save_user(user_id, user)

    name = _friend_display_name(user)
    if not st["history"]:
        await reply(
            f"⏪ {n} Nachrichten zurückgespult. {name} fängt beim nächsten "
            "Mal frisch an (gleiches Profil)."
        )
    else:
        await reply(
            f"⏪ {n} Nachrichten zurückgespult. {name} erinnert sich nicht "
            "mehr daran."
        )
