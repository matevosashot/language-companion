"""Generic, data-driven onboarding for the active friend profile.

`/setup` (or the "⚙️ Einrichten" button from /profile) walks the questions
declared in the active profile's profile.yaml. Each question is either free
`text` (optionally validated / length-capped) or a `choice` with allowed
answers shown as inline buttons. The collected answers are stored on the
profile's session state and rendered into the friend's first-message [PROFIL]
block (see friend.build_friend_first_message)."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import friend_profile
from .friend import cancel_pending_friend
from .handlers import is_allowed, is_group_chat
from .sessions import active_profile_id, load_user, profile_state, save_user

ONB_ASK = 0


def _current_q(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data["onb_questions"][context.user_data["onb_idx"]]


def _validate(q: dict, raw: str):
    """Return (value, error_message). value is None when error is set."""
    raw = (raw or "").strip()
    if not raw:
        if q.get("optional"):
            return "", None
        return None, "Bitte etwas eingeben:"
    if q.get("validate") == "int_range":
        lo, hi = q.get("min"), q.get("max")
        try:
            n = int(raw)
        except ValueError:
            return None, f"Bitte eine Zahl zwischen {lo} und {hi} eingeben:"
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            return None, f"Bitte eine Zahl zwischen {lo} und {hi} eingeben:"
        return n, None
    max_len = q.get("max_len")
    return (raw[:max_len] if max_len else raw), None


async def _ask_current(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    q = _current_q(context)
    prompt = q.get("prompt") or q.get("label") or q["id"]
    if q.get("type") == "choice":
        choices = q.get("choices") or []
        rows = [[InlineKeyboardButton(c, callback_data=f"onba:{i}")]
                for i, c in enumerate(choices)]
        await context.bot.send_message(
            chat_id, prompt, reply_markup=InlineKeyboardMarkup(rows)
        )
    else:
        await context.bot.send_message(chat_id, prompt)


async def _start_setup(context: ContextTypes.DEFAULT_TYPE,
                        user_id: int, chat_id: int) -> int:
    user = load_user(user_id)
    pid = active_profile_id(user)
    meta = friend_profile(pid)
    questions = meta.onboarding or []
    if not questions:
        await context.bot.send_message(
            chat_id,
            f"{meta.label} braucht kein Setup — schreib einfach los."
        )
        return ConversationHandler.END
    context.user_data["onb_user_id"] = user_id
    context.user_data["onb_pid"] = pid
    context.user_data["onb_questions"] = questions
    context.user_data["onb_answers"] = {}
    context.user_data["onb_idx"] = 0
    await context.bot.send_message(
        chat_id, f"Einrichtung für {meta.label}. (Abbruch jederzeit mit /cancel)"
    )
    await _ask_current(context, chat_id)
    return ONB_ASK


async def _store_and_advance(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                              qid: str, val) -> int:
    context.user_data["onb_answers"][qid] = val
    context.user_data["onb_idx"] += 1
    if context.user_data["onb_idx"] >= len(context.user_data["onb_questions"]):
        return await _finalize(context, chat_id)
    await _ask_current(context, chat_id)
    return ONB_ASK


async def _finalize(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int:
    user_id = context.user_data["onb_user_id"]
    pid = context.user_data["onb_pid"]
    answers = dict(context.user_data["onb_answers"])
    questions = context.user_data["onb_questions"]

    user = load_user(user_id)
    st = profile_state(user, pid)
    st["answers"] = answers
    st["history"] = []      # persona details changed → fresh conversation
    st["last_reply"] = None
    st["message_ids"] = []
    save_user(user_id, user)
    await cancel_pending_friend(user_id)

    meta = friend_profile(pid)
    labels = {q["id"]: (q.get("label") or q["id"]) for q in questions}
    lines = [f"Alles klar — {meta.label} ist eingerichtet:"]
    for k, v in answers.items():
        lines.append(f"• {labels.get(k, k)}: {v if v not in (None, '') else '(keine)'}")
    lines.append("\nSchreib was, dann fängt das Gespräch an.")
    await context.bot.send_message(chat_id, "\n".join(lines))
    context.user_data.clear()
    return ConversationHandler.END


# ---- entry points -------------------------------------------------

async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if is_group_chat(update):
        await update.message.reply_text(
            "Der Freund ist nur im Privatchat verfügbar. In der Gruppe bin ich "
            "nur der Lehrer."
        )
        return ConversationHandler.END
    if not is_allowed(update):
        await update.message.reply_text("Nicht autorisiert.")
        return ConversationHandler.END
    return await _start_setup(context, update.effective_user.id, update.effective_chat.id)


async def setup_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if is_group_chat(update) or not is_allowed(update):
        return ConversationHandler.END
    return await _start_setup(context, q.from_user.id, q.message.chat_id)


# ---- in-flight answer handlers ------------------------------------

async def onb_answer_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = _current_q(context)
    if q.get("type") == "choice":
        await update.message.reply_text("Bitte tippe auf eine der Optionen oben.")
        return ONB_ASK
    val, err = _validate(q, update.message.text or "")
    if err:
        await update.message.reply_text(err)
        return ONB_ASK
    return await _store_and_advance(context, update.effective_chat.id, q["id"], val)


async def onb_answer_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cq = update.callback_query
    await cq.answer()
    q = _current_q(context)
    if q.get("type") != "choice":
        return ONB_ASK
    try:
        i = int(cq.data.split(":", 1)[1])
        choice = (q.get("choices") or [])[i]
    except (ValueError, IndexError):
        return ONB_ASK
    await cq.edit_message_text(f"{q.get('label') or q['id']}: {choice}")
    return await _store_and_advance(context, cq.message.chat_id, q["id"], choice)


async def onb_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = _current_q(context)
    if not q.get("optional"):
        await update.message.reply_text("Diese Frage kann nicht übersprungen werden.")
        return ONB_ASK
    return await _store_and_advance(context, update.effective_chat.id, q["id"], "")


async def onb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Abgebrochen. Profil bleibt wie es war.")
    context.user_data.clear()
    return ConversationHandler.END


def build_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("setup", setup_start),
            CallbackQueryHandler(setup_start_cb, pattern=r"^onbsetup$"),
        ],
        states={
            ONB_ASK: [
                CommandHandler("skip", onb_skip),
                CallbackQueryHandler(onb_answer_choice, pattern=r"^onba:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, onb_answer_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", onb_cancel)],
    )
