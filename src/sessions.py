"""Per-user session storage. One JSON file per Telegram user under SESSIONS_DIR."""

import datetime as dt
import json

from .config import FRIEND_DEFAULT_LEVEL, SESSIONS_DIR

VALID_LEVELS = {"A1", "A2", "B1", "B2", "C1", "C2"}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _user_path(uid: int):
    return SESSIONS_DIR / f"{uid}.json"


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
        "user_level": FRIEND_DEFAULT_LEVEL,
    }


def load_user(uid: int) -> dict:
    path = _user_path(uid)
    if not path.exists():
        return _default_user()
    return json.loads(path.read_text())


def save_user(uid: int, data: dict) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _user_path(uid).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def iter_user_ids() -> list[int]:
    if not SESSIONS_DIR.exists():
        return []
    out = []
    for p in SESSIONS_DIR.glob("*.json"):
        try:
            out.append(int(p.stem))
        except ValueError:
            continue
    return out
