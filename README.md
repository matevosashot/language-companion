# Your Language Companion

A Telegram bot that pairs two **Claude Code** sessions as language-learning sidekicks: a `Freund` (friend) who chats naturally in German and a `Lehrer` (teacher) who silently grades each of your messages and corrects them when needed.

## Why this is nice

- **Two minds, kept apart.** The Freund and the Lehrer are independent Claude sessions with their own working dirs and `CLAUDE.md` system prompts. They never see each other's state, so the friend never breaks character to "teach" and the teacher never softens a correction to be polite. You get warm conversation in one channel and honest grading in another, at the same time.
- **In Telegram, not in a chat UI.** Conversations live in the same app you already use to text people. There's no separate website to open, no "start a session" ritual — you just write to a contact like you would to a human friend, and replies show up the same way (with typing indicators, voice notes, emoji reactions for the silent "OK, that was correct").
- **Real memory across days.** Both personas resume their Claude sessions via `--resume`, so the Freund remembers what you talked about last week and the Lehrer remembers which mistakes you keep making.
- **`/rewind` anything.** Said something weird and don't want the Freund to remember? Roll back N turns, persona intact.

## What it does

You send a message → two things happen in parallel:

1. **Freund** replies in character. You pick *which* friend with `/profile` (e.g. an everyday German buddy, or a quant-finance interview partner) — each is a separate persona with its own `CLAUDE.md` and its own Claude session. Profiles that support it carry per-profile details you fill in with `/setup` (the everyday friend asks name, age, city, …), plus a CEFR level (A1–C2) that gates vocabulary and sentence complexity.
2. **Lehrer** silently inspects the same message. If it's grammatically correct, you get a 👍 reaction. If not, you get an inline reply with the correction and explanation.

Both personas remember the conversation across restarts via Claude Code's `--resume <session_id>`. Each friend profile keeps its **own** session, so switching from one friend to another and back resumes the earlier conversation untouched. The active profile's history is checkpointed turn-by-turn so you can `/rewind` arbitrary numbers of turns.

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
- **`src/handlers.py`** — Telegram callbacks (`/profile`, `/reset`, `/rewind`, `/reload`, `/status`, `/reset_teacher`, message dispatch)
- **`src/onboarding.py`** — generic, data-driven `ConversationHandler` for `/setup` (reads the active profile's `profile.yaml` questions)
- **`src/voice.py`** — optional mlx-whisper transcription path
- **`src/idle.py`** — background loop that re-engages silent users
- **`src/app.py`** — wires everything together
- **`german/friend/<profile>/CLAUDE.md` + `profile.yaml`** and **`german/teacher/CLAUDE.md`** — persona system prompts (these are the actual "intelligence" of the bot — see "Friend profiles" and "Adapting" below). The top-level `german/` folder is the language pack; swap in a `french/`, `spanish/`, … sibling to host a different language and point `claude.friend.workdir` / `claude.teacher.workdir` at it.

### Session storage

One JSON file per Telegram user under the `sessions.dir` configured in `config.yaml`:

```
.telegram-german-sessions/
├── 12345678.json
└── 87654321.json
```

Each file holds that user's `active_friend_profile`, a `friend_profiles` map (per profile: its `history` of Claude session IDs + excerpts for `/rewind`, the onboarding `answers`, `last_reply`, and recent `message_ids`), a single shared `teacher` session ID, the global `user_level`, and last-seen timestamps. Old single-friend session files are migrated into this shape automatically the first time they're loaded (the existing Claude session is preserved, so the conversation resumes seamlessly). Concurrent writes from different users no longer share a file, and pruning a user is `rm sessions/<uid>.json`.

## Friend profiles

The friend is no longer a single persona — it's a set of **profiles**, each its own
subfolder under `claude.friend.workdir` (the *base* dir, `german/friend/`):

```
german/friend/
├── alltag/            # everyday German buddy (the default)
│   ├── CLAUDE.md      # the persona's system prompt
│   └── profile.yaml   # label + onboarding questions
└── quant/             # quant-finance interview partner (in German)
    ├── CLAUDE.md
    └── profile.yaml
```

A subfolder becomes a selectable profile as soon as it contains **both** a
`CLAUDE.md` and a `profile.yaml`. Profiles are discovered at startup; the configured
`claude.friend.default_profile` is the default (and the target old sessions migrate
into). Each profile keeps its **own** Claude session, so `/profile` switching never
crosses conversations. The single Lehrer and your `user_level` are shared across all
friend profiles — switching friends never disturbs grading.

`profile.yaml` describes the persona and its onboarding:

```yaml
label: "🇩🇪 Alltags-Freund"          # shown on the /profile button
description: "Lockerer deutscher Kumpel"
model: claude-sonnet-4-6             # optional; inherits claude.friend.model
default_language_level: B2           # optional fallback (user_level still wins)
onboarding:                          # may be empty → no /setup needed
  - id: name
    label: "Name"                    # used in the injected [PROFIL] block
    prompt: "Wie soll dein Freund heißen?"
    type: text                       # free text
    max_len: 40
  - id: age
    prompt: "Wie alt?"
    type: text
    validate: int_range              # with min/max
    min: 6
    max: 120
  - id: gender
    prompt: "Geschlecht?"
    type: choice                     # allowed answers → inline buttons
    choices: ["männlich", "weiblich", "divers"]
```

`/setup` walks these questions for the active profile; the answers are rendered into
the friend's first-message `[PROFIL]` block. To add a new friend, drop in a new
subfolder with these two files — no code changes.

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
| `/profile` | Pick / switch the friend persona (inline buttons; ✓ marks the active one). Each profile has its own Claude session. |
| `/setup` | Configure the active profile's details (data-driven from its `profile.yaml`). Re-runnable; resets that profile's conversation. |
| `/reset` | Reset the active friend's session (with confirmation). Keeps the profile's configured details. |
| `/reset_teacher` | Reset the Lehrer session (works in private and in groups) |
| `/level <A1–C2>` | Set the CEFR level the Lehrer grades against (resets the Lehrer session). Works in private and per-member in groups. |
| `/reload` | Re-read both `CLAUDE.md` files inside active sessions |
| `/reload_friend`, `/reload_teacher` | Reload just one |
| `/rewind [N]` | Forget the last N Freund turns of the active profile (default 1) |
| `/rewind list` | Interactive list with buttons |
| `/status` | Show active profile, available profiles, level, history depth, voice state |
| `/ping` | Sanity check |
| `/cancel` | Abort `/setup` |

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

1. **`<language>/friend/<profile>/CLAUDE.md` + `profile.yaml`** (e.g. `german/friend/alltag/`) — what each friend persona believes about itself, how it talks, and which details `/setup` asks for. Add or rewrite a profile by dropping in a subfolder (see "Friend profiles").
2. **`<language>/teacher/CLAUDE.md`** (e.g. `german/teacher/CLAUDE.md`) — what counts as an error, how to format corrections, when to output the `OK` marker.

To turn this into, say, a French tutor or a coding buddy:

- Add/rewrite friend profiles under `claude.friend.workdir` (each = a `CLAUDE.md` + `profile.yaml`) and rewrite the teacher `CLAUDE.md`.
- The friend's first-message framing (the `[PROFIL]` block + CEFR guidance) is built generically in `build_friend_first_message` in [src/friend.py](src/friend.py) from the profile's questions — usually no code change needed; the German wording lives there if you want to localize it.
- Update `build_teacher_prompt` in [src/teacher.py](src/teacher.py) if your "teacher" needs a different framing.
- Change `teacher.ok_marker` / `teacher.ok_reaction` in `config.yaml` if the silent-when-correct contract should differ.
- The onboarding flow ([src/onboarding.py](src/onboarding.py)) is data-driven — change the questions in each `profile.yaml`, not the code. The learner's CEFR level is set separately via `/level <A1–C2>` ([src/handlers.py](src/handlers.py)) and applies across all profiles.
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
