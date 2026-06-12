"""Config + .env loading. All tunables surface here as module-level constants."""

import logging
import os
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
ALLOWED_USERS = set(CFG["bot"].get("allowed_users") or [])

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

FRIEND_WORKDIR = _workdir(_FRIEND)
TEACHER_WORKDIR = _workdir(_TEACHER)
FRIEND_MODEL = _FRIEND.get("model")
TEACHER_MODEL = _TEACHER.get("model")
FRIEND_TOOLS = _tools(_FRIEND)
TEACHER_TOOLS = _tools(_TEACHER)
FRIEND_DEFAULT_LEVEL = _FRIEND["default_language_level"]
TEACHER_DEFAULT_LEVEL = _TEACHER["default_language_level"]

SESSIONS_DIR = SCRIPT_DIR / CFG["sessions"]["dir"]
FRIEND_HISTORY_MAX = int(CFG["sessions"]["friend_history_max"])

IDLE_PING_AFTER = int(CFG["proactivity"]["idle_ping_after_seconds"])
IDLE_CHECK_INTERVAL = int(CFG["proactivity"]["idle_check_interval_seconds"])
IDLE_PROMPT = CFG["proactivity"]["idle_prompt"]

TEACHER_OK_MARKER = CFG["teacher"]["ok_marker"]
TEACHER_OK_REACTION = CFG["teacher"]["ok_reaction"]

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
