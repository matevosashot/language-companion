# Your Language Companion

A Telegram bot that pairs two **Claude Code** sessions as language-learning sidekicks: a `Freund` (friend) who chats naturally in German and a `Lehrer` (teacher) who silently grades each of your messages and corrects them when needed.

## Why this is nice

- **Two minds, kept apart.** The Freund and the Lehrer are independent Claude sessions with their own working dirs and `CLAUDE.md` system prompts. They never see each other's state, so the friend never breaks character to "teach" and the teacher never softens a correction to be polite. You get warm conversation in one channel and honest grading in another, at the same time.
- **In Telegram, not in a chat UI.** Conversations live in the same app you already use to text people. There's no separate website to open, no "start a session" ritual — you just write to a contact like you would to a human friend, and replies show up the same way (with typing indicators, voice notes, emoji reactions for the silent "OK, that was correct").
- **Real memory across days.** Both personas resume their Claude sessions via `--resume`, so the Freund remembers what you talked about last week and the Lehrer remembers which mistakes you keep making.
- **`/rewind` anything.** Said something weird and don't want the Freund to remember? Roll back N turns, persona intact.

## What it does

You send a message → two things happen in parallel:

1. **Freund** replies in character. The persona has a profile (name, age, city, …) you set via `/new`, and a CEFR level (A1–C2) that gates vocabulary and sentence complexity.
2. **Lehrer** silently inspects the same message. If it's grammatically correct, you get a 👍 reaction. If not, you get an inline reply with the correction and explanation.

Both personas remember the conversation across restarts via Claude Code's `--resume <session_id>`. The Freund's history is checkpointed turn-by-turn so you can `/rewind` arbitrary numbers of turns.

Voice notes are transcribed locally with `mlx-whisper` (Apple Silicon optional) and then go through the same pipeline.

## Architecture

```
Telegram update
       │
       ▼
handlers.handle_message  ──►  handlers.dispatch_user_text
                                 │
                                 ├──► friend.friend_turn   (cancellable, queued)
                                 │       │
                                 │       └─► claude_runner.run_claude
                                 │             (Claude Code CLI subprocess)
                                 │
                                 └──► teacher.teacher_turn (stateless per turn)
                                         │
                                         └─► claude_runner.run_claude
```

- **`src/config.py`** — loads `.env` + `config.yaml` and exposes constants
- **`src/sessions.py`** — per-user JSON files in `.telegram-german-sessions/<uid>.json`
- **`src/claude_runner.py`** — async `subprocess_exec` wrapper around the `claude` CLI
- **`src/friend.py`** — Freund prompt building, per-user cancellation state, `friend_turn`
- **`src/teacher.py`** — Lehrer prompt building, `teacher_turn`
- **`src/markdown.py`** — markdown → Telegram-flavored HTML
- **`src/handlers.py`** — Telegram callbacks (`/rewind`, `/reload`, `/status`, `/new_teacher`, message dispatch)
- **`src/onboarding.py`** — `ConversationHandler` for `/new` and `/new_friend`
- **`src/voice.py`** — optional mlx-whisper transcription path
- **`src/idle.py`** — background loop that re-engages silent users
- **`src/app.py`** — wires everything together
- **`german/friend/CLAUDE.md`** and **`german/teacher/CLAUDE.md`** — persona system prompts (these are the actual "intelligence" of the bot — see "Adapting" below). The top-level `german/` folder is the language pack; swap in a `french/`, `spanish/`, … sibling to host a different language and point `claude.friend.workdir` / `claude.teacher.workdir` at it.

### Session storage

One JSON file per Telegram user under the `sessions.dir` configured in `config.yaml`:

```
.telegram-german-sessions/
├── 12345678.json
└── 87654321.json
```

Each file holds that user's `friend_history` (list of Claude session IDs + excerpts for `/rewind`), `teacher` (current teacher session ID), `friend_profile`, `user_level`, and last-seen timestamps. Concurrent writes from different users no longer share a file, and pruning a user is `rm sessions/<uid>.json`.

## Setup

```bash
pip install -r requirements.txt
# Optional, Apple Silicon only:
# pip install mlx-whisper
```

Create `.env`:

```
LANGUAGE_COMPANION_BOT_TOKEN=123456:abcdef...
```

Edit `config.yaml`:

- `bot.allowed_users` — Telegram user IDs allowed to talk to the bot in private (empty = open)
- `bot.allowed_chats` — group/supergroup chat IDs where teacher-only mode runs (empty = no groups)
- `teacher.group_level` — single CEFR level used for all members in group chats
- `claude.path` — path to the `claude` CLI binary
- `claude.friend_model` / `claude.teacher_model` — model IDs (e.g. `claude-sonnet-4-6`)
- `sessions.dir` — where per-user JSON files live
- `voice.enabled` — set `false` if you don't have mlx-whisper

Run:

```bash
python german-teacher-bot.py
```

## Commands

| Command | Action |
|---|---|
| `/new` | Onboard a fresh Freund + reset Lehrer session |
| `/new_friend` | Same, but keep the Lehrer session |
| `/new_teacher` | Reset only the Lehrer |
| `/level <A1–C2>` | Set the CEFR level the Lehrer grades against (resets the Lehrer session). Works in private and per-member in groups. |
| `/reload` | Re-read both `CLAUDE.md` files inside active sessions |
| `/reload_friend`, `/reload_teacher` | Reload just one |
| `/rewind [N]` | Forget the last N Freund turns (default 1) |
| `/rewind list` | Interactive list with buttons |
| `/status` | Show profile, level, history depth, voice state |
| `/ping` | Sanity check |
| `/cancel` | Abort onboarding |

## Group chats (teacher-only mode)

The bot can also sit in a **group chat as the Lehrer only** — the Freund persona is
completely disabled there. Members talk to each other in German and the teacher
reply-corrects each person's messages inline (or sets the 👀 reaction when a message is
fine), exactly like the private-chat teacher.

- **Per-member state.** Each member's teacher session and follow-up tracking are isolated
  per `(group, user)` and stored under `.telegram-german-sessions/groups/<chat_id>_<uid>.json`,
  separate from the private-chat files. The idle re-engagement loop never touches group files.
- **Context.** If a member replies to someone else's message, that message is passed to the
  teacher as conversational context so it can judge idiomaticity. Replying to the bot's own
  last correction opens a follow-up question (tutor mode).
- **Level.** Group members are graded at `teacher.group_level` by default; any member can set
  their own level with `/level <A1–C2>` (stored in their per-member file, resets their Lehrer
  session).
- **Access control.** The bot only operates in groups whose chat ID is listed in
  `bot.allowed_chats`; it stays silent everywhere else. Private chats remain gated by
  `bot.allowed_users`.
- **v1 limits.** Voice notes are ignored in groups; friend/rewind/onboarding commands are
  hidden and inert in groups.

### ⚠️ Required: disable Group Privacy in BotFather

By default a Telegram bot in **privacy mode** only receives commands, @mentions, and replies
to its own messages — so it would *not* see ordinary member chatter and could not correct it.
You must turn privacy off:

> BotFather → `/mybots` → select your bot → **Bot Settings → Group Privacy → Turn off**

Then **remove and re-add** the bot to any existing group (privacy state is captured at join
time). Making the bot a group admin also disables privacy. Without this, group mode does
nothing. Add the group's chat ID to `bot.allowed_chats` in `config.yaml`.

## Adapting to a different use case

The Python in `src/` is plumbing. The bot's personality lives in two places:

1. **`<language>/friend/CLAUDE.md`** (e.g. `german/friend/CLAUDE.md`) — what the friend persona believes about itself and how it should talk.
2. **`<language>/teacher/CLAUDE.md`** (e.g. `german/teacher/CLAUDE.md`) — what counts as an error, how to format corrections, when to output the `OK` marker.

To turn this into, say, a French tutor or a coding buddy:

- Rewrite both `CLAUDE.md` files for the new domain.
- Update `PROFILE_PREFIX` and `DEFAULT_PROFILE` in [src/friend.py](src/friend.py) — currently German-specific (CEFR levels, German instructions to the model).
- Update `build_teacher_prompt` in [src/teacher.py](src/teacher.py) if your "teacher" needs a different framing.
- Change `teacher.ok_marker` / `teacher.ok_reaction` in `config.yaml` if the silent-when-correct contract should differ.
- Replace the German onboarding strings and the CEFR level keyboard in [src/onboarding.py](src/onboarding.py).
- Adjust `proactivity.idle_prompt` in `config.yaml` for the re-engagement nudge tone.

If you don't want two personas, delete the teacher path: drop `teacher_turn` from `dispatch_user_text` in [src/handlers.py](src/handlers.py) and stop registering anything from [src/teacher.py](src/teacher.py).

## Voice

When `voice.enabled` is true and `mlx-whisper` is importable, voice notes are transcribed locally with the model in `voice.model` (default: `mlx-community/whisper-small-mlx`). The transcript is echoed back to you in italics, then goes through the normal Freund + Lehrer pipeline. Without `mlx-whisper` the bot replies with a friendly "not supported."

## Requirements

- Python 3.10+
- The `claude` CLI on `$PATH` (or set `claude.path` in `config.yaml`)
- `python-telegram-bot`, `mistune`, `pyyaml` (see `requirements.txt`)
- Optional: `mlx-whisper` for voice on Apple Silicon

## Credits

Inspired by [seedprod/claude-code-telegram](https://github.com/seedprod/claude-code-telegram) — same core idea (Claude Code CLI driving Telegram replies via `--resume`), but this is a **separate repo**, not a fork. The scope here is narrower (one specific bot, two personas, no plugin system) and the persona model is different (a profiled, opinionated friend + a strict but quiet teacher).
