"""/start onboarding for new users + /admin allowlist management (admin only)."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .. import allowlist
from .common import is_admin, is_allowed, is_allowed_chat, is_group_chat


# ============================================================
# /start — onboarding for new (not-yet-allowed) users
# ============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ungated entry point. Tells a not-yet-allowed user (or group) the chat_id
    the admin needs to add, with instructions. Confirms access if already set."""
    chat_id = update.effective_chat.id if update.effective_chat else None

    if is_group_chat(update):
        if is_allowed_chat(update):
            await update.message.reply_text(
                "✅ Dieser Gruppenchat ist freigeschaltet. Schreibt einfach los "
                "— /help erklärt, wie's funktioniert."
            )
            return
        await update.message.reply_text(
            "Dieser Gruppenchat ist noch nicht freigeschaltet.\n\n"
            f"Chat-ID: `{chat_id}`\n\n"
            "Gib diese ID dem Admin — er schaltet die Gruppe mit "
            "`/admin_add` frei.",
            parse_mode="Markdown",
        )
        return

    if is_allowed(update):
        await update.message.reply_text(
            "✅ Du bist freigeschaltet. Schreib einfach los, dann geht's los.\n\n"
            "ℹ️ /help erklärt, wie der Bot funktioniert und was du einstellen kannst."
        )
        return

    await update.message.reply_text(
        "👋 Willkommen! Damit du diesen Bot nutzen kannst, muss dich der "
        "Admin freischalten.\n\n"
        f"Deine Chat-ID: `{chat_id}`\n\n"
        "So geht's:\n"
        "1. Kopiere die Chat-ID oben.\n"
        "2. Schick sie dem Admin.\n"
        "3. Sobald du freigeschaltet bist, schreib mir einfach — "
        "/help zeigt dir dann alles.",
        parse_mode="Markdown",
    )


# ============================================================
# /admin — manage the allowlist (admin only)
# ============================================================

ADMIN_MENU_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("➕ Chat-ID hinzufügen", callback_data="adm:add")],
    [InlineKeyboardButton("📋 Erlaubte Chats anzeigen", callback_data="adm:list")],
])

_ADMIN_BACK_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("⬅️ Zurück", callback_data="adm:menu")],
])

_ADMIN_ADD_CANCEL_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("Abbrechen", callback_data="adm:addcancel")],
])

# Conversation state for the "add chat_id" flow (button → prompt → number).
ADMIN_ADD = 0


def _admin_list_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(str(cid), callback_data=f"adm:view:{cid}")]
            for cid, _meta in allowlist.items()]
    rows.append([InlineKeyboardButton("⬅️ Zurück", callback_data="adm:menu")])
    return InlineKeyboardMarkup(rows)


_ADMIN_LIST_TITLE = "📋 Erlaubte Chats — tippe eine ID zum Verwalten:"


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update) or not is_admin(update):
        return
    await update.message.reply_text("🔧 Admin-Menü", reply_markup=ADMIN_MENU_KB)


async def admin_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Benutzung: /admin_add <chat_id>")
        return
    try:
        cid = int(args[0])
    except ValueError:
        await update.message.reply_text(
            "Ungültige chat_id. Beispiel: /admin_add 123456789"
        )
        return
    if allowlist.add(cid, by=update.effective_user.id):
        await update.message.reply_text(f"✅ {cid} zur Allowlist hinzugefügt.")
    else:
        await update.message.reply_text(f"{cid} ist bereits freigeschaltet.")


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update):
        return
    data = q.data or ""

    if data == "adm:menu":
        await q.edit_message_text("🔧 Admin-Menü", reply_markup=ADMIN_MENU_KB)
        return

    if data == "adm:list":
        if not allowlist.items():
            await q.edit_message_text(
                "Keine freigeschalteten Chats.", reply_markup=_ADMIN_BACK_KB
            )
            return
        await q.edit_message_text(_ADMIN_LIST_TITLE, reply_markup=_admin_list_kb())
        return

    if data.startswith("adm:view:"):
        cid = data.split(":", 2)[2]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Löschen", callback_data=f"adm:del:{cid}"),
            InlineKeyboardButton("Abbrechen",  callback_data="adm:list"),
        ]])
        await q.edit_message_text(
            f"Chat-ID: {cid}\n\nWas möchtest du tun?", reply_markup=kb
        )
        return

    if data.startswith("adm:del:"):
        try:
            cid = int(data.split(":", 2)[2])
        except ValueError:
            return
        note = (f"🗑 {cid} entfernt." if allowlist.remove(cid)
                else f"{cid} war nicht in der Liste.")
        if allowlist.items():
            await q.edit_message_text(
                f"{note}\n\n{_ADMIN_LIST_TITLE}", reply_markup=_admin_list_kb()
            )
        else:
            await q.edit_message_text(
                f"{note}\n\nKeine freigeschalteten Chats mehr.",
                reply_markup=_ADMIN_BACK_KB,
            )
        return


# ---- "add chat_id" conversation (menu button → prompt → number) ----

async def admin_add_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if not is_admin(update):
        return ConversationHandler.END
    await q.edit_message_text(
        "Schick mir die Chat-ID, die ich freischalten soll (nur die Zahl).\n"
        "Abbrechen mit /cancel.",
        reply_markup=_ADMIN_ADD_CANCEL_KB,
    )
    return ADMIN_ADD


async def admin_add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    try:
        cid = int(raw)
    except ValueError:
        await update.message.reply_text(
            "Das ist keine gültige Chat-ID. Schick nur die Zahl "
            "(z.B. 123456789) oder /cancel."
        )
        return ADMIN_ADD
    if allowlist.add(cid, by=update.effective_user.id):
        msg = f"✅ {cid} zur Allowlist hinzugefügt."
    else:
        msg = f"{cid} ist bereits freigeschaltet."
    await update.message.reply_text(msg, reply_markup=ADMIN_MENU_KB)
    return ConversationHandler.END


async def admin_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Abgebrochen.", reply_markup=ADMIN_MENU_KB)
    return ConversationHandler.END


async def admin_add_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Abgebrochen.", reply_markup=ADMIN_MENU_KB)
    return ConversationHandler.END


def build_admin_handler() -> ConversationHandler:
    """Stateful 'add chat_id' flow started from the /admin menu button.
    Registered BEFORE the generic admin_callback so its entry (adm:add) wins."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_add_start_cb, pattern=r"^adm:add$"),
        ],
        states={
            ADMIN_ADD: [
                CommandHandler("cancel", admin_add_cancel),
                CallbackQueryHandler(admin_add_cancel_cb, pattern=r"^adm:addcancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_add_cancel)],
    )
