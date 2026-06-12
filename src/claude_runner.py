"""Async wrapper around the Claude Code CLI."""

import asyncio
import json
from dataclasses import dataclass
from typing import Optional

from .config import CLAUDE_PATH


@dataclass
class ClaudeResult:
    text: Optional[str] = None
    session_id: Optional[str] = None
    expired: bool = False
    cancelled: bool = False
    error: Optional[str] = None


async def run_claude(
    message: str,
    workdir: str,
    session_id: Optional[str],
    allowed_tools: str,
    proc_holder: Optional[dict] = None,
    model: Optional[str] = None,
) -> ClaudeResult:
    """Invoke Claude Code CLI asynchronously. `proc_holder` is a dict whose
    "proc" key will be set to the Process so callers can kill() it."""
    cmd = [
        CLAUDE_PATH, "-p", message,
        "--output-format", "json",
        "--allowedTools", allowed_tools,
    ]
    if model:
        cmd.extend(["--model", model])
    if session_id:
        cmd.extend(["--resume", session_id])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return ClaudeResult(error=str(e))

    if proc_holder is not None:
        proc_holder["proc"] = proc

    try:
        stdout_b, stderr_b = await proc.communicate()
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        return ClaudeResult(cancelled=True)

    if proc.returncode and proc.returncode < 0:
        return ClaudeResult(cancelled=True)

    raw = stdout_b.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        low = raw.lower() + " " + stderr_b.decode("utf-8", errors="replace").lower()
        if "no conversation found" in low or "session not found" in low:
            return ClaudeResult(expired=True)
        err = (stderr_b.decode("utf-8", errors="replace") or raw)[:500].strip()
        return ClaudeResult(error=err or f"exit {proc.returncode}")

    return ClaudeResult(text=data.get("result", ""), session_id=data.get("session_id"))
