"""Async wrapper around the Claude Agent SDK."""

import asyncio
import base64
import logging
import mimetypes
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    ResultMessage,
    StreamEvent,
    TextBlock,
    query,
)

from .config import CONTEXT_1M

logger = logging.getLogger(__name__)

# The CLI reports an unknown --resume target in prose. These markers mean
# "that session is gone, start fresh" rather than "something broke".
_EXPIRED_MARKERS = ("no conversation found", "session not found")

# Claude Code's own system prompt — what `claude -p` gave us before the SDK.
# Left unset, the SDK sends an *empty* system prompt instead. Measured: the
# personas' CLAUDE.md still loads with an empty prompt, so dropping this is an
# option, but it changes tool-use conventions and hides the date/environment
# block from the model — and the savings are cache reads, not fresh tokens.
_SYSTEM_PROMPT = {"type": "preset", "preset": "claude_code"}

# Prompt-side usage fields. Their sum equals the context size that
# `get_context_usage()` reports as totalTokens, so a finished turn tells us how
# full its own session is for free.
_PROMPT_TOKEN_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# The CLI ignores this unless auth is an API key; see the note in config.yaml.
_BETAS = ["context-1m-2025-08-07"] if CONTEXT_1M else []

COMPACT_PROMPT = "/compact"


@dataclass
class ClaudeResult:
    text: Optional[str] = None
    session_id: Optional[str] = None
    expired: bool = False
    cancelled: bool = False
    error: Optional[str] = None
    context_tokens: Optional[int] = None


def _build_options(
    workdir: str,
    allowed_tools: str,
    model: Optional[str],
    session_id: Optional[str],
    stderr: Optional[list[str]] = None,
    mcp_servers: Optional[dict[str, Any]] = None,
    extra_tools: Optional[list[str]] = None,
    partial: bool = False,
) -> ClaudeAgentOptions:
    tools = [t.strip() for t in allowed_tools.split(",") if t.strip()]
    # `Skill` in allowed_tools is deprecated; the skills option is the way in.
    skills = "all" if "Skill" in tools else None
    tools = [t for t in tools if t != "Skill"] + list(extra_tools or [])
    return ClaudeAgentOptions(
        cwd=workdir,
        allowed_tools=tools,
        mcp_servers=mcp_servers or {},
        skills=skills,
        system_prompt=_SYSTEM_PROMPT,
        model=model,
        resume=session_id,
        betas=_BETAS,
        stderr=stderr.append if stderr is not None else None,
        include_partial_messages=partial,
    )


def _classify(detail: str, stderr: list[str]) -> ClaudeResult:
    """Turn a failure into `expired` (the resumed session is gone) or `error`."""
    blob = (detail + " " + " ".join(stderr)).lower()
    if any(marker in blob for marker in _EXPIRED_MARKERS):
        return ClaudeResult(expired=True)
    err = (detail or "".join(stderr))[:500].strip()
    return ClaudeResult(error=err or "unknown error")


def _prompt_tokens(usage: Optional[dict[str, Any]]) -> Optional[int]:
    """Context size of the session this turn ran on, or None if unreported."""
    if not usage:
        return None
    values = [usage.get(k) for k in _PROMPT_TOKEN_KEYS]
    if all(v is None for v in values):
        return None
    return sum(v for v in values if isinstance(v, int))


def _image_block(path: Path) -> dict[str, Any]:
    media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data}}


async def _prompt_with_images(message: str, images: list[Path]) -> AsyncIterator[dict[str, Any]]:
    """One user message carrying the images ahead of the text. A single-message
    stream keeps stdin open until the result, so SDK tools still work."""
    content = [_image_block(p) for p in images] + [{"type": "text", "text": message}]
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


def _dispatch_stream_event(
    event: dict[str, Any],
    on_text: Optional[Callable[[str], None]],
    on_tool: Optional[Callable[[], None]],
) -> None:
    """Route one raw Anthropic stream event to the streaming hooks."""
    kind = event.get("type")
    if kind == "content_block_delta":
        delta = event.get("delta") or {}
        if on_text and delta.get("type") == "text_delta" and delta.get("text"):
            on_text(delta["text"])
    elif kind == "content_block_start":
        block = event.get("content_block") or {}
        if on_tool and block.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
            on_tool()


async def run_claude(
    message: str,
    workdir: str,
    session_id: Optional[str],
    allowed_tools: str,
    model: Optional[str] = None,
    images: Optional[list[Path]] = None,
    mcp_servers: Optional[dict[str, Any]] = None,
    extra_tools: Optional[list[str]] = None,
    on_text: Optional[Callable[[str], None]] = None,
    on_tool: Optional[Callable[[], None]] = None,
) -> ClaudeResult:
    """Run one Claude Code turn, resuming `session_id` when given. `images` are
    attached to the message; `mcp_servers`/`extra_tools` add in-process tools.
    `on_text` gets each text chunk as it streams and `on_tool` fires when a
    tool call starts; both are sync and must not block. Cancel the surrounding
    task to abort the turn."""
    stderr: list[str] = []
    streaming = on_text is not None or on_tool is not None
    options = _build_options(workdir, allowed_tools, model, session_id, stderr,
                             mcp_servers, extra_tools, partial=streaming)

    text_parts: list[str] = []
    prompt = _prompt_with_images(message, images) if images else message
    gen = query(prompt=prompt, options=options)
    try:
        try:
            async for msg in gen:
                if isinstance(msg, StreamEvent):
                    if msg.parent_tool_use_id is None:
                        _dispatch_stream_event(msg.event, on_text, on_tool)
                elif isinstance(msg, AssistantMessage):
                    text_parts.extend(
                        b.text for b in msg.content if isinstance(b, TextBlock)
                    )
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        return _classify(msg.result or "", stderr)
                    return ClaudeResult(
                        text=msg.result if msg.result is not None else "".join(text_parts),
                        session_id=msg.session_id,
                        context_tokens=_prompt_tokens(msg.usage),
                    )
        finally:
            # Close now rather than at async-generator finalization, so the CLI
            # subprocess dies with the turn. Shielded so it still runs while the
            # task is being cancelled.
            with anyio.move_on_after(5, shield=True):
                try:
                    await gen.aclose()
                except ClaudeSDKError:
                    pass
    except asyncio.CancelledError:
        return ClaudeResult(cancelled=True)
    except CLINotFoundError as e:
        return ClaudeResult(error=str(e))
    except ClaudeSDKError as e:
        return _classify(str(e), stderr)

    # Stream ended without a result message.
    return _classify("", stderr)


async def get_context(
    workdir: str,
    session_id: Optional[str],
    allowed_tools: str,
    model: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Context-window usage for `session_id`, the same data the CLI's /context
    shows. None when there is no session or it can no longer be resumed."""
    if not session_id:
        return None
    options = _build_options(workdir, allowed_tools, model, session_id)
    try:
        async with ClaudeSDKClient(options=options) as client:
            return await client.get_context_usage()
    except ClaudeSDKError as e:
        logger.warning("Context lookup failed for session %s: %s", session_id, e)
        return None


async def compact_session(
    workdir: str,
    session_id: str,
    allowed_tools: str,
    model: Optional[str] = None,
) -> ClaudeResult:
    """Run /compact on a session. Keeps the same session id and the
    conversation; returns empty `text`, so judge success by `error`."""
    return await run_claude(COMPACT_PROMPT, workdir, session_id, allowed_tools, model)
