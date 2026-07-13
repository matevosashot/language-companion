"""Telegram message dispatch + slash command callbacks."""

import asyncio
import datetime as dt
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ContextTypes

from .claude_runner import run_claude
from .config import (
    ALLOWED_CHATS,
    ALLOWED_USERS,
    DEFAULT_FRIEND_PROFILE,
    FRIEND_DEFAULT_LEVEL,
    FRIEND_HISTORY_MAX,
    FRIEND_PROFILES,
    TEACHER_GROUP_LEVEL,
    TEACHER_MODEL,
    TEACHER_TOOLS,
    TEACHER_WORKDIR,
    VOICE_ENABLED,
    friend_profile,
)
from .friend import (
    PENDING_FRIEND,
    PendingFriend,
    USER_LOCKS,
    cancel_pending_friend,
    friend_turn,
)
from .sessions import (
    VALID_LEVELS,
    active_profile_id,
    load_user,
    now_iso,
    profile_state,
    save_user,
)
from .teacher import GROUP_KONTEXT_LABEL, teacher_followup_turn, teacher_turn

INVALID_REPLY_MSG = (
    "❌ Antworten gehen nur an die letzte Lehrer-Korrektur. "
    "Schreib mir normal, oder antworte gezielt auf die letzte Korrektur."
)

logger = logging.getLogger(__name__)


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_USERS


def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat) and chat.type in ("group", "supergroup")


def is_allowed_chat(update: Update) -> bool:
    cid = update.effective_chat.id if update.effective_chat else None
    return cid in ALLOWED_CHATS


# ============================================================
# Dispatch
# ============================================================

CHAT_ACTION_REFRESH_S = 4


async def chat_action_keeper(
    app: Application,
    chat_id: int,
    friend_done: asyncio.Event,
    teacher_done: asyncio.Event,
) -> None:
    """Show 'typing…' until the friend reply lands, then 'choose_sticker…'
    until the teacher finishes. One keeper per dispatch."""
    while not friend_done.is_set():
        try:
            await app.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(friend_done.wait(), timeout=CHAT_ACTION_REFRESH_S)
        except asyncio.TimeoutError:
            pass
    while not teacher_done.is_set():
        try:
            await app.bot.send_chat_action(chat_id=chat_id, action="choose_sticker")
        except Exception:
            pass
        try:
            await asyncio.wait_for(teacher_done.wait(), timeout=CHAT_ACTION_REFRESH_S)
        except asyncio.TimeoutError:
            pass


QUOTE_MAX = 500


def _friend_text_with_quote(text: str, quoted: str) -> str:
    q = quoted.strip()[:QUOTE_MAX]
    return f"[Antwort auf deine frühere Nachricht: „{q}“]\n{text}"


async def dispatch_user_text(app: Application, user_id: int, chat_id: int,
                              message_id: int, text: str,
                              quoted: str | None = None) -> None:
    user = load_user(user_id)
    user["chat_id"] = chat_id
    user["last_message_ts"] = now_iso()
    user["idle_ping_sent_at"] = None
    save_user(user_id, user)

    # The friend sees a reference to the quoted message; the teacher keeps
    # evaluating the user's plain text against the latest friend reply.
    friend_text = _friend_text_with_quote(text, quoted) if quoted else text

    friend_done = asyncio.Event()
    teacher_done = asyncio.Event()

    async def _run_friend():
        try:
            await friend_turn(app, user_id, chat_id)
        finally:
            friend_done.set()

    async def _run_teacher():
        try:
            await teacher_turn(app, user_id, chat_id, message_id, text)
        finally:
            teacher_done.set()

    async with USER_LOCKS[user_id]:
        pending = PENDING_FRIEND.get(user_id)
        if pending is not None:
            queued = list(pending.queued) + [friend_text]
            excerpt = pending.excerpt or (text[:60])
            proc = pending.proc_holder.get("proc")
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            if pending.task is not None and not pending.task.done():
                pending.task.cancel()
            if pending.keeper_task is not None and not pending.keeper_task.done():
                pending.keeper_task.cancel()
        else:
            queued = [friend_text]
            excerpt = text[:60]

        new_pending = PendingFriend(queued=queued, excerpt=excerpt)
        PENDING_FRIEND[user_id] = new_pending
        new_pending.task = asyncio.create_task(_run_friend())
        new_pending.keeper_task = asyncio.create_task(
            chat_action_keeper(app, chat_id, friend_done, teacher_done)
        )

    asyncio.create_task(_run_teacher())


async def _run_with_teacher_keeper(app: Application, chat_id: int, coro) -> None:
    """Run a teacher-only coroutine while showing the 'choose_sticker' action.
    The friend event is pre-set so the keeper skips straight to the teacher
    phase — used for follow-ups and for all group-chat turns (no friend)."""
    friend_done = asyncio.Event()
    friend_done.set()
    teacher_done = asyncio.Event()
    keeper = asyncio.create_task(
        chat_action_keeper(app, chat_id, friend_done, teacher_done)
    )
    try:
        await coro
    finally:
        teacher_done.set()
        try:
            await keeper
        except asyncio.CancelledError:
            pass


async def _run_followup_with_keeper(app: Application, user_id: int, chat_id: int,
                                      message_id: int, text: str,
                                      state_chat_id: int | None = None,
                                      quoted_correction: str | None = None) -> None:
    await _run_with_teacher_keeper(
        app, chat_id,
        teacher_followup_turn(app, user_id, chat_id, message_id, text,
                              state_chat_id, quoted_correction=quoted_correction),
    )


async def handle_user_text(app: Application, update: Update, text: str) -> None:
    """Shared entry point for text and transcribed voice. Routes replies-to-
    teacher to a follow-up turn, rejects replies to anything else, otherwise
    performs the normal dual dispatch."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    replied = update.message.reply_to_message

    if replied is not None:
        user = load_user(user_id)
        last_teacher_id = user.get("last_teacher_message_id")
        teacher_ids = user.get("teacher_message_ids") or []
        is_teacher_reply = replied.message_id in teacher_ids or (
            last_teacher_id and replied.message_id == last_teacher_id
        )
        if is_teacher_reply:
            # When the user replies to an older correction (not the latest one),
            # quote it so the teacher knows which correction the follow-up means.
            quoted_correction = None
            if replied.message_id != last_teacher_id:
                quoted_correction = (replied.text or "").strip() or None
            asyncio.create_task(
                _run_followup_with_keeper(
                    app, user_id, chat_id, message_id, text,
                    quoted_correction=quoted_correction,
                )
            )
            return
        friend_ids = profile_state(user).get("message_ids") or []
        if replied.message_id in friend_ids:
            # Replying to the friend's latest message is just the natural next
            # turn — dispatch normally, no quote. Replying to an older friend
            # message: tell the friend which message is being answered.
            quoted = None
            if replied.message_id != friend_ids[-1]:
                quoted = (replied.text or "").strip() or None
            await dispatch_user_text(
                app, user_id, chat_id, message_id, text, quoted=quoted
            )
            return
        await update.message.reply_text(INVALID_REPLY_MSG)
        return

    await dispatch_user_text(app, user_id, chat_id, message_id, text)


async def handle_group_text(app: Application, update: Update, text: str) -> None:
    """Teacher-only entry point for group chats. The friend persona never runs.
    Each member's teacher state is isolated per (group, user). Corrects every
    member message inline (or 👀 when fine); replies to another member's
    message use that message as context; replies to the teacher's own last
    correction become a follow-up."""
    sender = update.effective_user
    if sender is None or sender.is_bot or not text:
        return
    user_id = sender.id
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    replied = update.message.reply_to_message

    user = load_user(user_id, chat_id)
    last_teacher_id = user.get("last_teacher_message_id")
    teacher_ids = user.get("teacher_message_ids") or []
    # Per-member /level choice wins; otherwise the configured group default.
    level = user.get("user_level") or TEACHER_GROUP_LEVEL

    if replied is not None:
        replied_from = replied.from_user
        is_teacher_reply = replied_from is not None and replied_from.is_bot and (
            replied.message_id in teacher_ids
            or (last_teacher_id and replied.message_id == last_teacher_id)
        )
        if is_teacher_reply:
            # Member is following up on one of their own corrections. Quote the
            # specific one when it isn't the latest.
            quoted_correction = None
            if replied.message_id != last_teacher_id:
                quoted_correction = (replied.text or "").strip() or None
            asyncio.create_task(_run_followup_with_keeper(
                app, user_id, chat_id, message_id, text, state_chat_id=chat_id,
                quoted_correction=quoted_correction,
            ))
            return
        # Replying to another human (or an older bot message): treat the
        # replied-to text as conversational context for the correction.
        context_text = (replied.text or "").strip() or None
        asyncio.create_task(_run_with_teacher_keeper(
            app, chat_id,
            teacher_turn(app, user_id, chat_id, message_id, text,
                         state_chat_id=chat_id, context_text=context_text,
                         level=level, kontext_label=GROUP_KONTEXT_LABEL),
        ))
        return

    asyncio.create_task(_run_with_teacher_keeper(
        app, chat_id,
        teacher_turn(app, user_id, chat_id, message_id, text,
                     state_chat_id=chat_id, level=level,
                     kontext_label=GROUP_KONTEXT_LABEL),
    ))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        if not is_allowed_chat(update):
            return  # stay silent in non-allowlisted groups
        await handle_group_text(context.application, update, update.message.text)
        return
    if not is_allowed(update):
        await update.message.reply_text("Nicht autorisiert.")
        return
    await handle_user_text(context.application, update, update.message.text)


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
# /reset_teacher
# ============================================================

def _state_chat_id(update: Update) -> int | None:
    """Group-scoped state key for the teacher; None in private chats."""
    return update.effective_chat.id if is_group_chat(update) else None


def _teacher_authorized(update: Update) -> bool:
    return is_allowed_chat(update) if is_group_chat(update) else is_allowed(update)


async def teacher_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _teacher_authorized(update):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text(
            "Benutzung: /teacher <Frage oder Nachricht an den Lehrer>"
        )
        return
    asyncio.create_task(_run_followup_with_keeper(
        context.application,
        update.effective_user.id,
        update.effective_chat.id,
        update.message.message_id,
        text,
        state_chat_id=_state_chat_id(update),
    ))


async def reset_teacher_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _teacher_authorized(update):
        return
    state_chat_id = _state_chat_id(update)
    user = load_user(update.effective_user.id, state_chat_id)
    user["teacher"] = None
    user["last_teacher_message_id"] = None
    user["teacher_message_ids"] = []
    save_user(update.effective_user.id, user, state_chat_id)
    await update.message.reply_text("Lehrer-Session zurückgesetzt.")


async def level_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the CEFR level the teacher grades against, then reset the teacher
    session so the new calibration starts clean. Works in private chats and in
    groups (scoped per member)."""
    if not _teacher_authorized(update):
        return
    arg = (context.args[0] if context.args else "").strip().upper()
    if arg not in VALID_LEVELS:
        levels = " / ".join(sorted(VALID_LEVELS))
        await update.message.reply_text(f"Benutzung: /level <{levels}>")
        return
    state_chat_id = _state_chat_id(update)
    user_id = update.effective_user.id
    user = load_user(user_id, state_chat_id)
    user["user_level"] = arg
    user["teacher"] = None
    user["last_teacher_message_id"] = None
    user["teacher_message_ids"] = []
    save_user(user_id, user, state_chat_id)
    await update.message.reply_text(
        f"Niveau auf {arg} gesetzt. Lehrer-Session zurückgesetzt — "
        "die nächste Nachricht wird auf diesem Niveau korrigiert."
    )


# ============================================================
# /reload — re-read CLAUDE.md inside an existing session
# ============================================================
#
# CLAUDE.md is baked into the system prompt at session start. To pick up
# changes without losing conversation history, we send one synthetic turn
# that tells the persona to Read CLAUDE.md again as a tool call. The
# freshly-read file lands in the conversation context and supersedes the
# baked-in copy from now on. The new session_id replaces the latest one
# in place — no new /rewind checkpoint is added.

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


# ============================================================
# /status, /ping
# ============================================================

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
