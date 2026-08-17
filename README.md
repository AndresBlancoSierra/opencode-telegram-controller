<p align="center">
  <a href="https://github.com/AndresBlancoSierra/opencode-telegram-controller">
    <img src="https://raw.githubusercontent.com/AndresBlancoSierra/opencode-telegram-controller/main/profile.svg" alt="OpenCode Telegram Controller — opencode-telegram-controller@arch">
  </a>
</p>

# OpenCode Telegram Controller — PC Control Bot

Control OpenCode *and your PC* from Telegram. Send a task as a plain message and
the bot runs `opencode` in one of your allowed projects and notifies you; use
the new `/status`, `/vpn`, `/docker_*`, `/screenshot`, `/lock`, `/reboot`, ... 
commands to monitor and manage the machine.

## Features

OpenCode:

- **Persistent sessions**: `/new` starts a session, plain messages continue it
  through the *same* real OpenCode session, `/history` lists past sessions, `/continue <id>`
  resumes one, `/current` shows the active session. Sessions survive bot restarts (SQLite).
- Natural-language message intake: any plain message is sent to the active session.
- Task queue with global concurrency limit and per-project **and per-session** serialization.
- Timeout and graceful cancellation (process-group SIGTERM/SIGKILL).
- Notifications: queued, started, progress, completed, failed, cancelled.
- Deterministic summaries (works offline, no LLM required); optional Ollama engine.
- SQLite persistence; running tasks are marked FAILED on restart.
- Long polling (no webhook, no exposed ports).

PC control (modular, audits every action):

- **System**: `/status` dashboard, `/resources`, `/disk`, `/processes`, `/health`.
- **Network**: `/ip`, `/dns`, `/network`.
- **VPN**: `/vpn [country]`, `/vpn_status`, `/vpn_dedicated`, `/vpn_change`
  (alias `/cambiar` — disconnect + reconnect, like the bash `cambiar` alias).
  NordVPN, country allowlist only.
- **Docker**: `/docker`, `/docker_status`, `/docker_restart <name>`, `/docker_logs <name> [lines]`
  (container name allowlist + strict validation).
- **Desktop**: `/screenshot`, `/windows`, `/lock` (Hyprland / grim / hyprlock).
- **Media**: `/photo` (camera), `/record_mic [seconds]` (microphone to MP3),
  `/stream` + `/stream_stop` (live screen clips via `wf-recorder`). Sending an
  audio file, a **voice note**, or a video file auto-plays it on the PC (audio
  in the background, video fullscreen, closes when done).
- **Power**: `/reboot`, `/shutdown`, `/sleep` guarded by `/confirm_*` within a
  timeout (and `/dismiss`).
- **Security**: per-user permission levels (READ / CONTROL / DESTRUCTIVE),
  read-only users optional, audit log table, no arbitrary command execution
  (fixed argv lists, `shell=False`, timeouts), secrets never logged.

## Requirements

- Arch Linux with systemd (user services). Verified: systemd 260.
- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- OpenCode CLI on `PATH` (verified against 1.18.4).
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and your numeric Telegram user ID.
- Optional per capability: `docker`, `nordvpn`, `grim`, `hyprctl`, `hyprlock`,
  `loginctl`, `mpv` (playback), `ffmpeg` (camera/mic), `wf-recorder` (stream).

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
/vpn us
/docker_status
/screenshot
/photo
/record_mic 15
/stream          →  /stream_stop
/lock
/reboot   →  /confirm_reboot   (within the confirmation timeout)
```

## Configuration

All settings are environment variables prefixed with `OTC_` (loaded from `.env`
or the systemd `EnvironmentFile`). See `.env.example` for the full list. The
projects allowlist lives in `config/projects.yaml`.

New PC-control settings (see `.env.example`):

- `OTC_VPN_PROVIDER=none|auto|nordvpn` (default `auto`)
- `OTC_VPN_COUNTRIES=us,germany` — country allowlist for `/vpn <country>`
- `OTC_VPN_DEDICATED_SERVER=` — fixed server for `/vpn_dedicated`
- `OTC_DOCKER_ALLOWED_CONTAINERS=` — container allowlist for restart/logs
- `OTC_DOCKER_LOGS_LINES=200`
- `OTC_SCREENSHOT_ENABLED=true|false`
- `OTC_CAMERA_DEVICE=/dev/video0`, `OTC_CAMERA_RESOLUTION=1280x720`
- `OTC_MIC_SOURCE=default`, `OTC_MIC_DEFAULT_SECONDS=10`, `OTC_MIC_MAX_SECONDS=120`
- `OTC_MEDIA_MAX_DOWNLOAD_MB=20`, `OTC_PLAYBACK_MAX_SECONDS=3600`
- `OTC_STREAM_CLIP_SECONDS=5`, `OTC_STREAM_FRAMERATE=15`, `OTC_STREAM_WITH_AUDIO=true`
- `OTC_POWER_CONFIRMATION_TIMEOUT_SECONDS=60`
- `OTC_POWER_REBOOT_COMMAND / _SHUTDOWN_ / _SLEEP_` (fixed argv, optional overrides)
- `OTC_HEALTH_CHECK_INTERVAL_SECONDS=0` (0 = proactive alerts disabled)
- `OTC_READ_ONLY_USER_IDS=` — users allowed to read but not control the PC

## Tests

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/integration
.venv/bin/python -m pytest tests/integration/   # real OpenCode (needs `opencode` on PATH)
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

265+ unit tests pass (they never contact Telegram and mock subprocesses); 3
integration tests run real OpenCode and take ~1 minute.
## Layout

```
src/opencode_telegram_controller/
├── bot.py            # aiogram router, middlewares, handlers, AppContext
├── auth.py           # Telegram user allowlist
├── projects.py       # project allowlist (config/projects.yaml)
├── queue_worker.py   # dispatch loop + concurrency + per-session serialization
├── task_executor.py  # per-task lifecycle (run, events, timeout, cancel, summary)
├── task_manager.py   # session lifecycle + high-level operations for handlers
├── core/             # process (safe runner), permissions, confirmations, audit
├── services/         # system, network, vpn, docker, desktop, media, stream, power, monitoring
├── commands/         # thin PC command handlers (system/network/vpn/docker/desktop/media/stream/power)
├── formatting.py     # OpenCode + PC formatting helpers
├── opencode/cli.py   # adapter: `opencode run --format json` / `opencode export`
├── summaries/        # deterministic (default) and ollama generators
├── notifications.py  # Telegram notifications (plain text, chunked)
├── database.py       # schema + migrations (tasks, sessions, audit_log)
├── repository.py     # SQLite task/session/user-state queries
└── main.py           # wiring + polling (entry point: `otc`)
```

Design details (managers, permissions, confirmations, audit, command security)
are in `docs/pc-control.md`.

## Security notes

- Only the Telegram user IDs in `OTC_ALLOWED_USER_IDS` can use the bot; users in
  `OTC_READ_ONLY_USER_IDS` can only read (no `/vpn`, `/docker_*`, `/lock`, power...).
- OpenCode only ever runs inside projects listed in `config/projects.yaml`.
- Prompts are passed as a single argv (never through a shell).
- PC capabilities never accept free shell input: fixed argv lists, `shell=False`,
  timeouts, and allowlist validation (VPN countries, Docker container names).
- Destructive power actions always require an in-memory confirmation (`/confirm_*`)
  that expires; confirmed and executed actions are written to the `audit_log` table.
- Secrets (`OTC_TELEGRAM_BOT_TOKEN`) live in `.env` / systemd `EnvironmentFile`, never in the repo.
- This project never commits or pushes to Git; it only reads repository state.
