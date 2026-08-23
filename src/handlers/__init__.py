"""Telegram message dispatch + slash command callbacks.

Split into focused submodules; this package re-exports the public API so
callers keep using ``from .handlers import <name>`` unchanged.
"""

from .admin import (
    admin_add_cmd,
    admin_callback,
    admin_cmd,
    build_admin_handler,
    start_cmd,
)
from .common import (
    is_admin,
    is_allowed,
    is_allowed_chat,
    is_group_chat,
)
from .dispatch import (
    handle_group_text,
    handle_message,
    handle_user_text,
)
from .friend_cmds import (
    profile_callback,
    profile_cmd,
    reset_callback,
    reset_cmd,
    rewind_callback,
    rewind_cmd,
)
from .help import help_cmd
from .reload import (
    reload_cmd,
    reload_friend_cmd,
    reload_teacher_cmd,
)
from .status import ping_cmd, status_cmd
from .teacher_cmds import level_cmd, reset_teacher_cmd, teacher_cmd

__all__ = [
    "admin_add_cmd",
    "admin_callback",
    "admin_cmd",
    "build_admin_handler",
    "handle_group_text",
    "handle_message",
    "handle_user_text",
    "help_cmd",
    "is_admin",
    "is_allowed",
    "is_allowed_chat",
    "is_group_chat",
    "level_cmd",
    "ping_cmd",
    "profile_callback",
    "profile_cmd",
    "reload_cmd",
    "reload_friend_cmd",
    "reload_teacher_cmd",
    "reset_callback",
    "reset_cmd",
    "reset_teacher_cmd",
    "rewind_callback",
    "rewind_cmd",
    "start_cmd",
    "status_cmd",
    "teacher_cmd",
]
