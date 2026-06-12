"""Onboarding ConversationHandler for /new and /new_friend."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import FRIEND_DEFAULT_LEVEL
from .friend import cancel_pending_friend
from .handlers import is_allowed
from .sessions import VALID_LEVELS, load_user, save_user

ONB_CONFIRM, ONB_NAME, ONB_AGE, ONB_GENDER, ONB_CITY, ONB_EXTRAS, ONB_LEVEL = range(7)

CONFIRM_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("Ja, neu starten", callback_data="onbc:yes"),
    InlineKeyboardButton("Abbrechen",       callback_data="onbc:no"),
]])

GENDER_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("Männlich", callback_data="g:männlich"),
    InlineKeyboardButton("Weiblich", callback_data="g:weiblich"),
    InlineKeyboardButton("Divers",  callback_data="g:divers"),
]])

LEVEL_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton(lv, callback_data=f"lv:{lv}") for lv in ("A1", "A2", "B1")],
    [InlineKeyboardButton(lv, callback_data=f"lv:{lv}") for lv in ("B2", "C1", "C2")],
])


def _is_new_command(text: str) -> bool:
    """True if the entry command was /new (not /new_friend)."""
    if not text:
        return False
    first = text.split()[0].split("@")[0]
    return first == "/new"


async def onb_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_allowed(update):
        await update.message.reply_text("Nicht autorisiert.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    is_new = _is_new_command(update.message.text or "")
    context.user_data["onb_clear_teacher"] = is_new

    user = load_user(user_id)
    has_friend = bool(user.get("friend_profile"))
    has_teacher = bool(user.get("teacher"))

    # Nothing to lose yet — skip the confirmation prompt.
    if not (has_friend or (is_new and has_teacher)):
        return await _begin_onboarding(update, context)

    if is_new:
        warn = (
            "Das setzt deinen aktuellen Freund UND die Lehrer-Session zurück. "
            "Der bisherige Gesprächsverlauf geht verloren."
        )
    else:
        warn = (
            "Das setzt deinen aktuellen Freund zurück. "
            "Der bisherige Gesprächsverlauf geht verloren."
        )
    await update.message.reply_text(
        warn + "\n\nWirklich neu starten?",
        reply_markup=CONFIRM_KB,
    )
    return ONB_CONFIRM


async def onb_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "onbc:no":
        await q.edit_message_text("Abgebrochen. Alles bleibt wie es war.")
        context.user_data.clear()
        return ConversationHandler.END
    if q.data != "onbc:yes":
        return ONB_CONFIRM
    await q.edit_message_text("Alles klar, neu aufsetzen.")
    return await _begin_onboarding(update, context)


async def _begin_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user = load_user(user_id)
    context.user_data["onb_prev_profile"] = user.get("friend_profile")
    context.user_data["onb"] = {}
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "Lass uns deinen deutschen Freund einrichten.\n"
            "Wie soll er/sie heißen? (Abbruch mit /cancel)"
        ),
    )
    return ONB_NAME


async def onb_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()[:40]
    if not name:
        await update.message.reply_text("Bitte einen Namen eingeben:")
        return ONB_NAME
    context.user_data["onb"]["name"] = name
    await update.message.reply_text("Wie alt soll er/sie sein? (Zahl)")
    return ONB_AGE


async def onb_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int((update.message.text or "").strip())
        if not 6 <= age <= 120:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Bitte eine Zahl zwischen 6 und 120 eingeben:")
        return ONB_AGE
    context.user_data["onb"]["age"] = age
    await update.message.reply_text("Geschlecht?", reply_markup=GENDER_KB)
    return ONB_GENDER


async def onb_gender_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("g:"):
        return ONB_GENDER
    gender = q.data.split(":", 1)[1]
    context.user_data["onb"]["gender"] = gender
    await q.edit_message_text(f"Geschlecht: {gender}")
    await context.bot.send_message(
        chat_id=q.message.chat_id,
        text="Wo wohnt er/sie? (Stadt oder Region in DE/AT/CH)",
    )
    return ONB_CITY


async def onb_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    city = (update.message.text or "").strip()[:60]
    if not city:
        await update.message.reply_text("Bitte eine Stadt eingeben:")
        return ONB_CITY
    context.user_data["onb"]["city"] = city
    await update.message.reply_text(
        "Noch was Besonderes? Beruf, Hobbys, Eigenheiten? (oder /skip)"
    )
    return ONB_EXTRAS


async def onb_extras(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onb"]["extras"] = (update.message.text or "").strip()[:500]
    return await _ask_level(update, context)


async def onb_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["onb"]["extras"] = ""
    return await _ask_level(update, context)


async def _ask_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Und zum Schluss: welches Deutsch-Niveau hast DU gerade?\n"
        "Das hilft mir, Korrekturen und Wortschatz passend zu wählen.\n"
        "(A1 = Anfänger · B1 = solide Mittelstufe · C2 = fast wie Muttersprachler)",
        reply_markup=LEVEL_KB,
    )
    return ONB_LEVEL


async def onb_level_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not q.data or not q.data.startswith("lv:"):
        return ONB_LEVEL
    level = q.data.split(":", 1)[1]
    if level not in VALID_LEVELS:
        return ONB_LEVEL
    context.user_data["onb"]["level"] = level
    await q.edit_message_text(f"Niveau: {level}")
    return await _finalize_onboarding(update, context)


async def _finalize_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    onb = dict(context.user_data["onb"])
    level = onb.pop("level", FRIEND_DEFAULT_LEVEL)
    profile = onb

    user = load_user(user_id)
    user["friend_profile"] = profile
    user["user_level"] = level
    user["friend_history"] = []  # fresh start with new persona
    user["last_friend_reply"] = None
    if context.user_data.get("onb_clear_teacher"):
        user["teacher"] = None
    save_user(user_id, user)

    await cancel_pending_friend(user_id)

    msg = (
        f"Alles klar — dein neuer Freund:\n"
        f"• Name: {profile['name']}\n"
        f"• Alter: {profile['age']}\n"
        f"• Geschlecht: {profile['gender']}\n"
        f"• Stadt: {profile['city']}\n"
        f"• Besonderheiten: {profile.get('extras') or '(keine)'}\n"
        f"• Dein Niveau: {level}"
    )
    if context.user_data.get("onb_clear_teacher"):
        msg += "\n\nLehrer-Session auch zurückgesetzt."
    msg += "\n\nSchreib was, dann fängt das Gespräch an."
    await update.message.reply_text(msg)
    context.user_data.clear()
    return ConversationHandler.END


async def onb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    prev = context.user_data.get("onb_prev_profile")
    if prev is not None:
        user = load_user(update.effective_user.id)
        user["friend_profile"] = prev
        save_user(update.effective_user.id, user)
    await update.message.reply_text("Abgebrochen. Profil bleibt wie es war.")
    context.user_data.clear()
    return ConversationHandler.END


def build_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("new_friend", onb_start),
            CommandHandler("new", onb_start),
        ],
        states={
            ONB_CONFIRM: [CallbackQueryHandler(onb_confirm_cb, pattern=r"^onbc:")],
            ONB_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_name)],
            ONB_AGE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_age)],
            ONB_GENDER: [CallbackQueryHandler(onb_gender_cb, pattern=r"^g:")],
            ONB_CITY:   [MessageHandler(filters.TEXT & ~filters.COMMAND, onb_city)],
            ONB_EXTRAS: [
                CommandHandler("skip", onb_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, onb_extras),
            ],
            ONB_LEVEL:  [CallbackQueryHandler(onb_level_cb, pattern=r"^lv:")],
        },
        fallbacks=[CommandHandler("cancel", onb_cancel)],
    )
