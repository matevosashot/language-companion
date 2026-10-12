"""Photos: storing what the user sends, holding uncaptioned photos until the
next message, and the friend's in-process tools for finding and sending web
photos (Wikimedia Commons search + URL download)."""

import asyncio
import base64
import html
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool
from telegram import Bot, Message

from .config import SESSIONS_DIR

logger = logging.getLogger(__name__)

PHOTO_DIR = SESSIONS_DIR / "photos"
# Downloaded user photos are only needed until the friend turn has run (the
# image then lives in the Claude session), so old files are pruned.
PHOTO_KEEP_S = 7 * 86400

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia rejects requests whose User-Agent carries no contact URL.
USER_AGENT = "language-companion-bot/1.0 (https://github.com/matevosashot/language-companion)"
HTTP_TIMEOUT_S = 15
SEND_MAX_BYTES = 10 * 1024 * 1024   # Telegram's send_photo limit
THUMB_MAX_BYTES = 2 * 1024 * 1024
SEND_WIDTH = 1280
THUMB_WIDTH = 330                    # one of Wikimedia's standard thumb widths
SEARCH_MAX = 6
OUTBOX_MAX = 3                       # photos per friend turn
SENDABLE_TYPES = ("image/jpeg", "image/png", "image/webp")

SERVER_NAME = "fotos"


# ------------------------------------------------------------------
# User → friend
# ------------------------------------------------------------------

def _prune(dir_: Path) -> None:
    cutoff = time.time() - PHOTO_KEEP_S
    for f in dir_.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


async def download_user_photo(bot: Bot, message: Message, user_id: int) -> Path:
    """Save the photo (largest size) or image document to disk and return it."""
    if message.photo:
        item, ext = message.photo[-1], ".jpg"
    else:
        item = message.document
        ext = {"image/png": ".png", "image/webp": ".webp",
               "image/gif": ".gif"}.get(item.mime_type or "", ".jpg")
    dir_ = PHOTO_DIR / str(user_id)
    dir_.mkdir(parents=True, exist_ok=True)
    _prune(dir_)
    path = dir_ / f"{item.file_unique_id}{ext}"
    file = await bot.get_file(item.file_id)
    await file.download_to_drive(path)
    return path


# Uncaptioned photos wait here for the user's next message.
STAGED: dict[int, list[Path]] = {}


def stage(user_id: int, path: Path) -> None:
    STAGED.setdefault(user_id, []).append(path)


def take_staged(user_id: int) -> list[Path]:
    return [p for p in STAGED.pop(user_id, []) if p.exists()]


# ------------------------------------------------------------------
# Friend → user
# ------------------------------------------------------------------

@dataclass
class OutPhoto:
    data: bytes
    caption: str = ""


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def _error(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _short(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return str(e) or type(e).__name__


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


async def _fetch(client: httpx.AsyncClient, url: str, max_bytes: int) -> tuple[bytes, str]:
    """GET `url`, refusing bodies over `max_bytes`. Returns (body, content-type)."""
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        body = bytearray()
        async for chunk in r.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError(f"Bild zu groß (> {max_bytes // (1024 * 1024)} MB)")
        return bytes(body), ctype


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_S, follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


async def _commons_search(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    r = await client.get(COMMONS_API, params={
        "action": "query", "format": "json",
        "generator": "search", "gsrnamespace": 6,
        "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": limit,
        "prop": "imageinfo", "iiprop": "url|mime|extmetadata",
        "iiurlwidth": SEND_WIDTH,
        "iiextmetadatafilter": "ImageDescription|ObjectName",
        "iiextmetadatalanguage": "de",
    })
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or {}
    hits = []
    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        meta = info.get("extmetadata") or {}
        desc = _strip_html((meta.get("ImageDescription") or meta.get("ObjectName")
                            or {}).get("value", ""))
        title = page.get("title", "").removeprefix("File:").removeprefix("Datei:")
        thumb = url.replace(f"/{SEND_WIDTH}px-", f"/{THUMB_WIDTH}px-")
        hits.append({"title": title, "desc": desc[:150], "url": url, "thumb": thumb})
    return hits


async def _thumb_block(client: httpx.AsyncClient, url: str) -> Optional[dict[str, Any]]:
    try:
        data, ctype = await _fetch(client, url, THUMB_MAX_BYTES)
    except Exception:
        return None
    if ctype not in SENDABLE_TYPES:
        return None
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"),
            "mimeType": ctype}


def build_photo_server(outbox: list[OutPhoto]) -> tuple[Any, list[str]]:
    """In-process MCP server with the friend's photo tools, bound to one turn's
    `outbox`. Tools never send anything themselves: the turn sends the outbox
    after its text, and a cancelled turn just drops it."""

    @tool(
        "bild_suchen",
        "Sucht echte Fotos auf Wikimedia Commons. Liefert nummerierte Treffer "
        "mit Titel, Beschreibung, URL und einem kleinen Vorschaubild, damit du "
        "das passende auswählen kannst. Danach mit foto_senden verschicken. "
        "Suchbegriffe funktionieren auf Englisch oft besser.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Wonach gesucht wird"},
                "limit": {"type": "integer", "description": f"Anzahl Treffer (max {SEARCH_MAX})"},
            },
            "required": ["query"],
        },
    )
    async def bild_suchen(args: dict[str, Any]) -> dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            return _error("Leere Suche.")
        limit = max(1, min(int(args.get("limit") or 5), SEARCH_MAX))
        try:
            async with _client() as client:
                hits = await _commons_search(client, query, limit)
                thumbs = await asyncio.gather(*(_thumb_block(client, h["thumb"]) for h in hits))
        except Exception as e:
            logger.warning("Commons search failed for %r: %s", query, e)
            return _error(f"Suche fehlgeschlagen: {_short(e)}")
        if not hits:
            return _text("Keine Treffer. Versuch andere (z. B. englische) Suchbegriffe.")
        content: list[dict[str, Any]] = []
        for i, (h, thumb) in enumerate(zip(hits, thumbs), 1):
            line = f"{i}. {h['title']}"
            if h["desc"]:
                line += f" — {h['desc']}"
            content.append({"type": "text", "text": f"{line}\nURL: {h['url']}"})
            if thumb:
                content.append(thumb)
        return {"content": content}

    @tool(
        "foto_senden",
        "Schickt dem Nutzer ein Foto per Telegram, zusammen mit deiner Antwort "
        "(das Foto kommt direkt nach deinem Text). Die URL muss direkt auf eine "
        "Bilddatei (JPEG/PNG/WebP) zeigen, z. B. eine URL aus bild_suchen.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Direkte Bild-URL (http/https)"},
                "caption": {"type": "string", "description": "Optionale kurze Bildunterschrift"},
            },
            "required": ["url"],
        },
    )
    async def foto_senden(args: dict[str, Any]) -> dict[str, Any]:
        url = (args.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.I):
            return _error("Nur http(s)-URLs.")
        if len(outbox) >= OUTBOX_MAX:
            return _error(f"Schon {OUTBOX_MAX} Fotos in dieser Nachricht — das reicht.")
        try:
            async with _client() as client:
                data, ctype = await _fetch(client, url, SEND_MAX_BYTES)
        except Exception as e:
            return _error(f"Konnte das Bild nicht laden ({_short(e)}). Nimm eine andere URL.")
        if ctype not in SENDABLE_TYPES:
            return _error(f"Das ist kein JPEG/PNG/WebP-Bild (Content-Type: {ctype or '?'}). "
                          "Nimm eine direkte Bild-URL.")
        outbox.append(OutPhoto(data=data, caption=(args.get("caption") or "").strip()[:1000]))
        return _text("Ok, das Foto wird mit deiner Nachricht verschickt.")

    server = create_sdk_mcp_server(SERVER_NAME, tools=[bild_suchen, foto_senden])
    names = [f"mcp__{SERVER_NAME}__bild_suchen", f"mcp__{SERVER_NAME}__foto_senden"]
    return server, names


async def send_outbox(bot: Bot, chat_id: int, outbox: list[OutPhoto]) -> list[int]:
    """Send the turn's photos as Telegram photos; returns the sent message ids."""
    ids = []
    for photo in outbox:
        try:
            sent = await bot.send_photo(chat_id=chat_id, photo=photo.data,
                                        caption=photo.caption or None)
            ids.append(sent.message_id)
        except Exception:
            logger.exception("Sending friend photo failed")
    return ids
