"""Shared authorization predicates and small helpers used across handlers."""

from telegram import Update

from .. import allowlist
from ..config import ADMIN_IDS


def is_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid in ADMIN_IDS


def is_allowed(update: Update) -> bool:
    """Private-chat access: admins always, otherwise the chat_id must be on the
    file-backed allowlist. In a private chat effective_chat.id == the user id."""
    if is_admin(update):
        return True
    cid = update.effective_chat.id if update.effective_chat else None
    return cid is not None and allowlist.contains(cid)


def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat) and chat.type in ("group", "supergroup")


def is_allowed_chat(update: Update) -> bool:
    """Group access: the group's chat_id must be explicitly on the allowlist.
    (Admin membership alone does not authorize a whole group.)"""
    cid = update.effective_chat.id if update.effective_chat else None
    return cid is not None and allowlist.contains(cid)


def _state_chat_id(update: Update) -> int | None:
    """Group-scoped state key for the teacher; None in private chats."""
    return update.effective_chat.id if is_group_chat(update) else None


def _teacher_authorized(update: Update) -> bool:
    return is_allowed_chat(update) if is_group_chat(update) else is_allowed(update)
