"""/help — explains how the bot works and what can be configured.
Context-aware: group chats see teacher-only help; private chats see the full
friend + teacher command set, plus an admin section for admins."""

from telegram import Update
from telegram.ext import ContextTypes

from ..config import PHOTOS_ENABLED, VOICE_ENABLED
from .common import is_admin, is_allowed, is_allowed_chat, is_group_chat

_GROUP_HELP = (
    "🤖 So funktioniert dieser Bot (Gruppe)\n\n"
    "In Gruppen ist nur der 🧑‍🏫 Lehrer aktiv (kein Freund). Er korrigiert "
    "jede Nachricht inline — oder reagiert mit 👀, wenn alles passt. "
    "Antworte (Reply) auf eine Korrektur, um dazu nachzufragen — das geht "
    "auch bei älteren Korrekturen.\n\n"
    "Befehle:\n"
    "/teacher <Frage> — den Lehrer direkt etwas fragen\n"
    "/level <A1–C2> — dein Deutsch-Niveau setzen (gilt nur für dich)\n"
    "/reset_teacher — deine Lehrer-Session zurücksetzen\n"
    "/status — aktueller Status\n"
    "/context — Kontext-Auslastung der Lehrer-Session\n"
    "/ping — Erreichbarkeit prüfen"
)


def _private_help(admin: bool) -> str:
    voice = "an" if VOICE_ENABLED else "aus"
    photos = (
        "📷 Fotos: mit Text → der Freund antwortet (der Lehrer prüft den Text); "
        "ohne Text → kommt mit deiner nächsten Nachricht. Der Freund kann dir "
        "auch Fotos schicken." if PHOTOS_ENABLED else "📷 Fotos: aus"
    )
    text = (
        "🤖 So funktioniert dieser Bot\n\n"
        "Du hast zwei Begleiter gleichzeitig:\n"
        "• 👤 Freund — chattet locker auf Deutsch, damit du im Alltag übst.\n"
        "• 🧑‍🏫 Lehrer — korrigiert im Hintergrund jede deiner Nachrichten "
        "(oder 👀, wenn alles passt). Antworte (Reply) auf eine Korrektur, "
        "um nachzufragen — auch bei älteren Korrekturen.\n\n"
        "👤 Freund:\n"
        "/profile — Gesprächspartner wählen/wechseln\n"
        "/setup — aktuellen Freund einrichten\n"
        "/reset — Gespräch zurücksetzen (Profil bleibt)\n"
        "/rewind [N|list] — die letzten N Freund-Nachrichten zurückspulen\n"
        "/reload_friend — Persona-Datei (CLAUDE.md) neu laden\n\n"
        "🧑‍🏫 Lehrer:\n"
        "/teacher <Frage> — den Lehrer direkt etwas fragen\n"
        "/level <A1–C2> — dein Deutsch-Niveau setzen\n"
        "/reset_teacher — Lehrer-Session zurücksetzen\n"
        "/reload_teacher — Lehrer-Persona neu laden\n"
        "/reload — beide Personas neu laden\n\n"
        "Sonstiges:\n"
        "/status — aktueller Sitzungsstatus\n"
        "/context — Kontext-Auslastung von Freund + Lehrer\n"
        f"/ping — Erreichbarkeit  ·  🎤 Sprachnachrichten: {voice}\n"
        f"{photos}\n\n"
        "⚙️ Konfiguration (für den Betreiber):\n"
        "• Freund-Profile: german/friend/<profil>/ "
        "(je CLAUDE.md + profile.yaml)\n"
        "• Lehrer: german/teacher/CLAUDE.md\n"
        "• Zugang & Admins: config.yaml, Allowlist der Chats separat.\n"
        "Nach dem Ändern einer CLAUDE.md: /reload."
    )
    if admin:
        text += (
            "\n\n🔧 Admin:\n"
            "/admin — Menü: Chats freischalten / anzeigen / entfernen\n"
            "/admin_add <chat_id> — Chat direkt freischalten\n"
            "Neue Nutzer bekommen ihre Chat-ID über /start."
        )
    return text


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        if not is_allowed_chat(update):
            return  # stay silent in non-allowlisted groups
        await update.message.reply_text(_GROUP_HELP)
        return
    if not is_allowed(update):
        return  # unauthorized private users only get /start
    await update.message.reply_text(_private_help(is_admin(update)))
