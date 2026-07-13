"""Per-user session storage. One JSON file per Telegram user under SESSIONS_DIR."""

import datetime as dt
import json

from .config import DEFAULT_FRIEND_PROFILE, SESSIONS_DIR

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
        # Friend state is now per-profile. `active_friend_profile` is the id of
        # the profile the user is currently talking to (None → the configured
        # default). `friend_profiles[id]` holds that persona's own session.
        "active_friend_profile": None,
        "friend_profiles": {},
        "teacher": None,        # ONE teacher session, shared across friend profiles
        "last_message_ts": None,
        "idle_ping_sent_at": None,
        "chat_id": None,
        "last_teacher_message_id": None,
        "teacher_message_ids": [],  # recent teacher corrections that stay repliable
        "user_level": None,  # None = not chosen yet; readers fall back to a default
    }


def _default_profile_state() -> dict:
    return {
        "history": [],      # list of {sid, user_excerpt, ts} (was friend_history)
        "answers": None,    # onboarding answers (was friend_profile)
        "last_reply": None, # last friend text (was last_friend_reply)
        "message_ids": [],  # recent friend message ids (was friend_message_ids)
    }


def _migrate(data: dict) -> dict:
    """Lazily fold an old flat-friend session into the per-profile shape.

    Old sessions stored friend_history / friend_profile / last_friend_reply /
    friend_message_ids at the top level, implicitly belonging to the single
    friend persona. Wrap them into the default profile so the live Claude
    session (its session ids) resumes seamlessly."""
    if "friend_profiles" in data:
        return data
    old_keys = ("friend_history", "friend_profile",
                "last_friend_reply", "friend_message_ids")
    if not any(k in data for k in old_keys):
        # Brand-new dict that simply predates these keys — nothing to fold.
        data.setdefault("friend_profiles", {})
        data.setdefault("active_friend_profile", None)
        return data
    default = DEFAULT_FRIEND_PROFILE
    data["friend_profiles"] = {default: {
        "history":     data.pop("friend_history", []) or [],
        "answers":     data.pop("friend_profile", None),
        "last_reply":  data.pop("last_friend_reply", None),
        "message_ids": data.pop("friend_message_ids", []) or [],
    }}
    data["active_friend_profile"] = default
    return data


def load_user(uid: int, chat_id: int | None = None) -> dict:
    path = _user_path(uid, chat_id)
    if not path.exists():
        return _default_user()
    return _migrate(json.loads(path.read_text()))


def active_profile_id(user: dict) -> str:
    return user.get("active_friend_profile") or DEFAULT_FRIEND_PROFILE


def profile_state(user: dict, pid: str | None = None) -> dict:
    """Return the per-profile friend state for `pid` (default: the active
    profile), creating an empty one in `user` if it doesn't exist yet. Mutates
    `user` in place — the caller is responsible for save_user()."""
    pid = pid or active_profile_id(user)
    profiles = user.setdefault("friend_profiles", {})
    st = profiles.get(pid)
    if st is None:
        st = _default_profile_state()
        profiles[pid] = st
    return st


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
