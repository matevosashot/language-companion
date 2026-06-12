"""Markdown -> Telegram HTML rendering."""

import html

import mistune


class TelegramRenderer(mistune.HTMLRenderer):
    def heading(self, text, level, **a): return f"<b>{text}</b>\n\n"
    def paragraph(self, text): return f"{text}\n\n"
    def list(self, text, ordered, **a): return text + "\n"
    def list_item(self, text, **a): return f"• {text}\n"
    def block_code(self, code, info=None):
        return f"<pre>{html.escape(code.strip())}</pre>\n\n"
    def codespan(self, text): return f"<code>{html.escape(text)}</code>"
    def emphasis(self, text): return f"<i>{text}</i>"
    def strong(self, text): return f"<b>{text}</b>"
    def link(self, text, url, title=None):
        return f'<a href="{html.escape(url)}">{text}</a>'
    def linebreak(self): return "\n"


_md = mistune.create_markdown(
    renderer=TelegramRenderer(escape=False),
    plugins=["strikethrough", "url"],
)


def to_telegram_html(text: str) -> str:
    return _md(text).strip()
