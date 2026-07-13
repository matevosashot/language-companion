"""File-backed allowlist of chat_ids permitted to use the bot.

One unified list governs access: in private chats the id is the user's private
chat id (== their Telegram user id), in groups it's the group's chat id. The
admin (config `bot.admin_ids`) is always allowed and is NOT stored here.

The in-memory dict is the read path; every mutation persists to ALLOWLIST_FILE
(read-modify-write, single process). Call load() once at startup."""

import json

from .config import ALLOWLIST_FILE
from .sessions import now_iso

# chat_id -> {"added_at": iso, "added_by": admin_user_id | None}
_chats: dict[int, dict] = {}


def load() -> None:
    """(Re)load the allowlist from disk into memory. Missing/corrupt file → empty."""
    _chats.clear()
    if not ALLOWLIST_FILE.exists():
        return
    try:
        data = json.loads(ALLOWLIST_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return
    for k, v in (data.get("chats") or {}).items():
        try:
            _chats[int(k)] = v or {}
        except (ValueError, TypeError):
            continue


def _save() -> None:
    ALLOWLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"chats": {str(cid): meta for cid, meta in sorted(_chats.items())}}
    ALLOWLIST_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def contains(cid: int) -> bool:
    return cid in _chats


def add(cid: int, by: int | None = None) -> bool:
    """Add cid to the allowlist. Returns False if it was already present."""
    if cid in _chats:
        return False
    _chats[cid] = {"added_at": now_iso(), "added_by": by}
    _save()
    return True


def remove(cid: int) -> bool:
    """Remove cid from the allowlist. Returns False if it wasn't present."""
    if cid not in _chats:
        return False
    del _chats[cid]
    _save()
    return True


def items() -> list[tuple[int, dict]]:
    return sorted(_chats.items())
