"""Deterministic correction markup for the teacher.

The teacher decides *what* the corrected sentence is; this module works out
*how* to show it, by diffing the user's text against the correction word by
word:

- changed or inserted words  -> <u>**neu**</u>   (the old form is not shown)
- removed words              -> ~~alt~~
- a moved word               -> ~~Wort~~ at the old place, <u>**Wort**</u> at the new
- a pure typo inside a word  -> only the changed letters: H<u>**a**</u>use

The teacher reaches it through the in-process tool `korrektur_markieren`. It
copies the result into its reply as the first line, and may call the tool
again with a different correction or touch the line up by hand."""

import difflib
import re
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

SERVER_NAME = "korrektur"
TOOL_NAME = f"mcp__{SERVER_NAME}__korrektur_markieren"

# Words keep inner hyphens, apostrophes and number separators ("geht's",
# "E-Mail", "19:00", "3,5"); every other non-space character is its own token.
_TOKEN_RE = re.compile(r"\w+(?:[-'’:.,/]\w+)*|[^\w\s]")
_NO_SPACE_BEFORE = set(".,!?;:)]}…%“‘")
_NO_SPACE_AFTER = set("([{„‚")
_FINAL_PUNCT = set(".!?")
# A one-word change counts as a typo (letter-level marks) only when it is a
# couple of letters and not an ending: meine -> meinen and verstehen ->
# verstanden are grammar and get the whole word marked.
_TYPO_MAX_EDITS = 2
# Two words this similar are "the same word, corrected" when aligning a
# changed stretch; less similar ones count as a different word.
_SAME_WORD_RATIO = 0.5
# Alignment costs: a corrected form of the same word is cheap; swapping in a
# different word still beats delete + insert, because a replaced word is
# shown as just the new word.
_COST_SAME, _COST_SWAP, _COST_GAP = 0.5, 1.5, 1.0

Item = tuple[str, str, str]   # (display, first raw token, last raw token)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _u(s: str) -> str:
    return f"<u>**{s}**</u>"


def _s(s: str) -> str:
    return f"~~{s}~~"


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def _is_inflection(old: str, new: str) -> bool:
    prefix = _common_prefix_len(old.lower(), new.lower())
    return prefix >= 2 and prefix >= min(len(old), len(new)) - 1


def _edits(old: str, new: str) -> int:
    ops = difflib.SequenceMatcher(None, old.lower(), new.lower(), autojunk=False).get_opcodes()
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in ops if op != "equal")


def _similar(old: str, new: str) -> bool:
    return difflib.SequenceMatcher(None, old.lower(), new.lower()).ratio() >= _SAME_WORD_RATIO


def _mark_letters(old: str, new: str) -> str:
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(new[j1:j2])
        elif op == "delete":
            out.append(_s(old[i1:i2]))
        else:  # insert / replace: show only the new letters
            out.append(_u(new[j1:j2]))
    return "".join(out)


def _mark_word(old: str, new: str) -> str:
    """One word replaced by one word: letter marks for a typo, else whole word."""
    if (old.isalpha() and new.isalpha() and not _is_inflection(old, new)
            and _edits(old, new) <= _TYPO_MAX_EDITS):
        return _mark_letters(old, new)
    return _u(new)


def _join(items: list[Item]) -> str:
    """Re-space the items the way German text is spaced (deleted tokens have
    no place in the corrected text's whitespace, so spacing is rebuilt)."""
    out = ""
    prev_last = None
    open_quote = False
    for display, first, last in items:
        if prev_last is not None:
            tight = (first in _NO_SPACE_BEFORE or prev_last in _NO_SPACE_AFTER
                     or (first == '"' and open_quote)
                     or (prev_last == '"' and open_quote))
            if not tight:
                out += " "
        out += display
        for tok in {first, last}:
            if tok == '"':
                open_quote = not open_quote
        prev_last = last
    return out


def _align(old: list[str], new: list[str]) -> list[tuple[str, int, int]]:
    """Token alignment inside a changed stretch (edit distance with fuzzy
    substitution): ("sub", i, j), ("del", i, -1) or ("ins", -1, j)."""
    n, m = len(old), len(new)

    def sub_cost(i: int, j: int) -> float:
        return _COST_SAME if _similar(old[i], new[j]) else _COST_SWAP

    cost = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 or j == 0:
                cost[i][j] = (i + j) * _COST_GAP
                continue
            cost[i][j] = min(cost[i - 1][j - 1] + sub_cost(i - 1, j - 1),
                             cost[i - 1][j] + _COST_GAP,
                             cost[i][j - 1] + _COST_GAP)
    ops = []
    i, j = n, m
    while i or j:
        if i and j and cost[i][j] == cost[i - 1][j - 1] + sub_cost(i - 1, j - 1):
            ops.append(("sub", i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i and cost[i][j] == cost[i - 1][j] + _COST_GAP:
            ops.append(("del", i - 1, -1))
            i -= 1
        else:
            ops.append(("ins", -1, j - 1))
            j -= 1
    return ops[::-1]


def mark_correction(original: str, corrected: str) -> str:
    """The corrected text with every change against `original` marked."""
    a, b = _tokens(original), _tokens(corrected)
    # Match case-insensitively: "Ich gehe" -> "Gestern bin ich" keeps "ich"
    # unmarked instead of flagging the capital letter.
    opcodes = difflib.SequenceMatcher(
        None, [t.lower() for t in a], [t.lower() for t in b], autojunk=False,
    ).get_opcodes()
    # Words that newly appear somewhere: an old word replaced here but placed
    # elsewhere was *moved*, so its old spot is struck through.
    new_words = {t.lower() for op, _, _, j1, j2 in opcodes if op != "equal" for t in b[j1:j2]}

    items: list[Item] = []
    pending: list[str] = []   # consecutive inserted tokens share one underline

    def flush() -> None:
        if pending:
            items.append((_u(_join([(t, t, t) for t in pending])), pending[0], pending[-1]))
            pending.clear()

    def struck(tok: str) -> None:
        flush()
        items.append((_s(tok), tok, tok))

    for op, i1, i2, j1, j2 in opcodes:
        if op == "equal":
            flush()
            items += [(t, t, t) for t in b[j1:j2]]
        elif op == "delete":
            for tok in a[i1:i2]:
                struck(tok)
        elif op == "insert":
            pending += b[j1:j2]
        else:
            for kind, i, j in _align(a[i1:i2], b[j1:j2]):
                if kind == "del":
                    struck(a[i1 + i])
                elif kind == "ins":
                    pending.append(b[j1 + j])
                else:
                    old, new = a[i1 + i], b[j1 + j]
                    if old.lower() in new_words and old.lower() != new.lower():
                        struck(old)
                        pending.append(new)
                    elif _mark_word(old, new) == _u(new):
                        pending.append(new)
                    else:
                        flush()
                        items.append((_mark_word(old, new), new, new))
    flush()
    # An added final full stop stays unmarked: missing end punctuation is not
    # a mistake in chat.
    if items:
        display, first, last = items[-1]
        if last in _FINAL_PUNCT and display.endswith(f"{last}**</u>"):
            inner = display[len("<u>**"):-len(f"{last}**</u>")].rstrip()
            items[-1:] = ([(_u(inner), first, first)] if inner else []) + [(last, last, last)]
    # Strike-throughs of neighbouring words read better as one stretch.
    out: list[Item] = []
    for item in items:
        if out and item[0].startswith("~~") and out[-1][0].startswith("~~"):
            prev = out.pop()
            item = (_s(f"{prev[0][2:-2]} {item[0][2:-2]}"), prev[1], item[2])
        out.append(item)
    return _join(out)


def build_correction_server(original: str, calls: list[str]) -> tuple[Any, list[str]]:
    """In-process MCP server with the teacher's markup tool, bound to the
    user's text of this turn. Each result is appended to `calls` (for logs)."""

    @tool(
        "korrektur_markieren",
        "Erzeugt die Korrekturzeile: vergleicht den Originaltext des Nutzers "
        "Wort für Wort mit deiner korrigierten Fassung und markiert jede "
        "Änderung (unterstrichen = geändert/neu, durchgestrichen = gestrichen, "
        "bei Tippfehlern nur die Buchstaben). Gib nur deine korrigierte "
        "Fassung als reinen Text an — den Originaltext kennt das Tool schon.",
        {
            "type": "object",
            "properties": {
                "korrigiert": {
                    "type": "string",
                    "description": "Der vollständige korrigierte Text des Nutzers, "
                                   "reiner Text ohne jede Markierung",
                },
            },
            "required": ["korrigiert"],
        },
    )
    async def korrektur_markieren(args: dict[str, Any]) -> dict[str, Any]:
        corrected = (args.get("korrigiert") or "").strip()
        if not corrected:
            return {"content": [{"type": "text", "text": "Leerer Text."}], "is_error": True}
        line = mark_correction(original, corrected)
        calls.append(line)
        return {"content": [{"type": "text", "text": line}]}

    return create_sdk_mcp_server(SERVER_NAME, tools=[korrektur_markieren]), [TOOL_NAME]
