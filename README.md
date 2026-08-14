# OpenCode Telegram Controller

Control OpenCode on this PC from Telegram. Send a task as a plain message, the
bot runs `opencode` in one of your allowed projects, notifies you when it
starts and finishes, and sends a summary (duration, model, tokens, changed
files, test results).

## Features

- **Persistent sessions**: `/new` starts a session, plain messages continue it
  through the *same* real OpenCode session (`opencode run --session`), `/history`
  lists past sessions, `/continue <id>` resumes one, `/current` shows the active
  session. Sessions survive bot restarts (stored in SQLite).
- Natural-language message intake: any plain message is sent to the active session.
- Commands: `/status`, `/tasks [all]`, `/task <id>`, `/cancel [id]`, `/logs [id]`,
  `/projects`, `/use <name>`, `/new`, `/history`, `/continue <id>`, `/current`, `/help`.
- Task queue with global concurrency limit and per-project **and per-session** serialization.
- Timeout and graceful cancellation (process-group SIGTERM/SIGKILL).
- Notifications: queued, started, progress, completed, failed, cancelled.
  Interactive (in-session) messages skip "queued/started/completed" spam; the
  final reply is delivered as the bot's answer.
- Deterministic summaries (works offline, no LLM required); optional Ollama engine.
- SQLite persistence; running tasks are marked FAILED on restart.
- Long polling (no webhook, no exposed ports).
- Security: Telegram user allowlist, project allowlist, no shell execution, no Git auto-commit.

## Requirements

- Arch Linux with systemd (user services). Verified: systemd 260.
- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- OpenCode CLI on `PATH` (verified against 1.18.4).
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and your numeric Telegram user ID.

## Install

```bash
cd ~/Proyects/opencode-telegram-controller
uv sync
cp .env.example .env        # set OTC_TELEGRAM_BOT_TOKEN and OTC_ALLOWED_USER_IDS
cp config/projects.yaml.example config/projects.yaml   # enable the projects you want
.venv/bin/otc               # run in foreground first
```

Run it as a user service:

```bash
scripts/install-service.sh
systemctl --user status opencode-telegram-controller
journalctl --user -u opencode-telegram-controller -f
```

## Usage

Message the bot from Telegram:

```
/start
/status
/new
Fix the failing tests in this project and create a commit.
/current
/history
```

## Configuration

All settings are environment variables prefixed with `OTC_` (loaded from `.env`
or the systemd `EnvironmentFile`). See `.env.example` for the full list. The
projects allowlist lives in `config/projects.yaml`.

## Tests

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/integration
.venv/bin/python -m pytest tests/integration/   # real OpenCode (needs `opencode` on PATH)
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

146 unit tests pass (they never contact Telegram and mock subprocesses); 3
integration tests run real OpenCode and take ~1 minute.

## Layout

```
src/opencode_telegram_controller/
├── bot.py            # aiogram router, middlewares, handlers (/new /history /continue /current)
├── auth.py           # Telegram user allowlist
├── projects.py       # project allowlist (config/projects.yaml)
├── queue_worker.py   # dispatch loop + concurrency + per-session serialization
├── task_executor.py  # per-task lifecycle (run, events, timeout, cancel, summary, session backfill)
├── task_manager.py   # session lifecycle + high-level operations for handlers
├── opencode/cli.py   # adapter: `opencode run --format json` / `opencode export` / session_exists
├── summaries/        # deterministic (default) and ollama generators
├── notifications.py  # Telegram notifications (plain text, chunked)
├── database.py       # schema + migrations (tasks + sessions tables)
├── repository.py     # SQLite task/session/user-state queries
└── main.py           # wiring + polling (entry point: `otc`)
```

See the Spanish overview in the Obsidian vault:
`~/Documents/obsidian/Proyects/OpenCode-Telegram-Controller.md`.

## Security notes

- Only the Telegram user IDs in `OTC_ALLOWED_USER_IDS` can use the bot.
- OpenCode only ever runs inside projects listed in `config/projects.yaml`.
- Prompts are passed as a single argv (never through a shell).
- Secrets (`OTC_TELEGRAM_BOT_TOKEN`) live in `.env` / systemd `EnvironmentFile`, never in the repo.
- This project never commits or pushes to Git; it only reads repository state.
