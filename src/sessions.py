"""Per-user session storage. One JSON file per Telegram user under SESSIONS_DIR."""

import datetime as dt
import json

from .config import SESSIONS_DIR

VALID_LEVELS = {"A1", "A2", "B1", "B2", "C1", "C2"}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _user_path(uid: int, chat_id: int | None = None):
    # Private chats: SESSIONS_DIR/{uid}.json (unchanged). Group chats: keyed by
    # (chat, user) under a groups/ subdir so each member has isolated teacher
    # state and so the idle loop (iter_user_ids) never enumerates them.
    if chat_id is None:
        return SESSIONS_DIR / f"{uid}.json"
    return SESSIONS_DIR / "groups" / f"{chat_id}_{uid}.json"


def _default_user() -> dict:
    return {
        "friend_history": [],
        "teacher": None,
        "last_message_ts": None,
        "idle_ping_sent_at": None,
        "chat_id": None,
        "friend_profile": None,
        "last_friend_reply": None,
        "friend_message_ids": [],
        "last_teacher_message_id": None,
        "user_level": None,  # None = not chosen yet; readers fall back to a default
    }


def load_user(uid: int, chat_id: int | None = None) -> dict:
    path = _user_path(uid, chat_id)
    if not path.exists():
        return _default_user()
    return json.loads(path.read_text())


def save_user(uid: int, data: dict, chat_id: int | None = None) -> None:
    path = _user_path(uid, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def iter_user_ids() -> list[int]:
    # Private-chat users only: non-recursive glob deliberately skips the
    # groups/ subdir so the idle pinger never re-engages group members.
    if not SESSIONS_DIR.exists():
        return []
    out = []
    for p in SESSIONS_DIR.glob("*.json"):
        try:
            out.append(int(p.stem))
        except ValueError:
            continue
    return out
