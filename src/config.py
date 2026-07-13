"""Config + .env loading. All tunables surface here as module-level constants."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env(SCRIPT_DIR / ".env")
CFG = yaml.safe_load((SCRIPT_DIR / "config.yaml").read_text())

BOT_TOKEN = os.environ.get(CFG["bot"]["token_env"]) or os.environ.get("TELEGRAM_BOT_TOKEN")
# Admins are always allowed and manage the allowlist; they are NOT stored in it.
ADMIN_IDS = set(CFG["bot"].get("admin_ids") or [])
# Everyone else must be an allowed chat_id, kept in this JSON file (not config).
ALLOWLIST_FILE = SCRIPT_DIR / (
    CFG["bot"].get("allowlist_file") or ".telegram-german-sessions/allowed_chats.json"
)

_CLAUDE = CFG["claude"]
CLAUDE_PATH = _CLAUDE["path"]
_DEFAULT_TOOLS = _CLAUDE.get("allowed_tools") or []


def _persona(key: str) -> dict:
    return _CLAUDE.get(key) or {}


def _tools(persona: dict) -> str:
    return ",".join(persona.get("allowed_tools") or _DEFAULT_TOOLS)


def _workdir(persona: dict) -> str:
    return str((SCRIPT_DIR / persona["workdir"]).resolve())


_FRIEND = _persona("friend")
_TEACHER = _persona("teacher")

TEACHER_WORKDIR = _workdir(_TEACHER)
# Friend defaults — used as fallbacks for profiles that omit an override.
FRIEND_MODEL = _FRIEND.get("model")
TEACHER_MODEL = _TEACHER.get("model")
FRIEND_TOOLS = _tools(_FRIEND)
TEACHER_TOOLS = _tools(_TEACHER)
FRIEND_DEFAULT_LEVEL = _FRIEND["default_language_level"]
TEACHER_DEFAULT_LEVEL = _TEACHER["default_language_level"]

# ------------------------------------------------------------------
# Friend profiles
#
# The friend `workdir` is now a BASE directory; each subfolder that contains
# both a CLAUDE.md and a profile.yaml is a selectable persona with its own
# Claude session. Metadata (label, onboarding questions, optional model/level
# overrides) lives in that profile.yaml.
# ------------------------------------------------------------------

FRIEND_BASE_WORKDIR = _workdir(_FRIEND)
DEFAULT_FRIEND_PROFILE = _FRIEND.get("default_profile") or "alltag"


@dataclass
class FriendProfile:
    id: str
    workdir: str
    label: str
    description: str = ""
    model: str | None = None
    level: str = FRIEND_DEFAULT_LEVEL
    tools: str = FRIEND_TOOLS
    onboarding: list = field(default_factory=list)


def _load_friend_profiles() -> dict[str, FriendProfile]:
    profiles: dict[str, FriendProfile] = {}
    base = Path(FRIEND_BASE_WORKDIR)
    if not base.exists():
        return profiles
    for sub in sorted(base.iterdir()):
        if not sub.is_dir():
            continue
        if not ((sub / "CLAUDE.md").exists() and (sub / "profile.yaml").exists()):
            continue
        meta = yaml.safe_load((sub / "profile.yaml").read_text()) or {}
        tools_list = meta.get("allowed_tools")
        profiles[sub.name] = FriendProfile(
            id=sub.name,
            workdir=str(sub.resolve()),
            label=meta.get("label") or sub.name,
            description=meta.get("description") or "",
            model=meta.get("model") or FRIEND_MODEL,
            level=meta.get("default_language_level") or FRIEND_DEFAULT_LEVEL,
            tools=",".join(tools_list) if tools_list else FRIEND_TOOLS,
            onboarding=meta.get("onboarding") or [],
        )
    return profiles


FRIEND_PROFILES = _load_friend_profiles()


def friend_profile(pid: str | None) -> FriendProfile:
    """Resolve a profile id to its FriendProfile, falling back to the default."""
    if pid and pid in FRIEND_PROFILES:
        return FRIEND_PROFILES[pid]
    if DEFAULT_FRIEND_PROFILE in FRIEND_PROFILES:
        return FRIEND_PROFILES[DEFAULT_FRIEND_PROFILE]
    # Last resort: any discovered profile (keeps the bot usable if the configured
    # default is missing; app.py validates this loudly at startup).
    return next(iter(FRIEND_PROFILES.values()))

SESSIONS_DIR = SCRIPT_DIR / CFG["sessions"]["dir"]
FRIEND_HISTORY_MAX = int(CFG["sessions"]["friend_history_max"])

IDLE_PING_AFTER = int(CFG["proactivity"]["idle_ping_after_seconds"])
IDLE_CHECK_INTERVAL = int(CFG["proactivity"]["idle_check_interval_seconds"])
IDLE_PROMPT = CFG["proactivity"]["idle_prompt"]

TEACHER_OK_MARKER = CFG["teacher"]["ok_marker"]
TEACHER_OK_REACTION = CFG["teacher"]["ok_reaction"]
TEACHER_GROUP_LEVEL = CFG["teacher"].get("group_level") or TEACHER_DEFAULT_LEVEL

VOICE_CFG_ENABLED = bool(CFG["voice"]["enabled"])
VOICE_MODEL = CFG["voice"]["model"]

logging.basicConfig(
    level=CFG["logging"]["level"],
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("german_bot")

try:
    if VOICE_CFG_ENABLED:
        import mlx_whisper  # noqa: F401
        VOICE_ENABLED = True
    else:
        VOICE_ENABLED = False
except ImportError:
    VOICE_ENABLED = False
