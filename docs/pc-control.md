# PC Control — design notes

This document describes the PC-control layer added on top of the OpenCode
controller: managers, permissions, confirmations, audit and command security.

## Architecture

Every capability maps to a *manager* behind a stable interface. Managers are
the only place that talk to the OS (always through `core.process.CommandRunner`,
which runs fixed argv lists with `shell=False`, enforces timeouts and kills the
process group on expiry — **no capability ever accepts a raw command string**
from Telegram).

```
Telegram message
  └─ commands/<group>.register(router)     thin handlers (parse + render)
        └─ services/<manager>              semantic operations (validated)
             └─ core/process.CommandRunner fixed argv, no shell, timeouts
                  └─ OS binaries (docker, nordvpn, hyprctl, grim, loginctl, ...)
```

All managers are wired into a single `AppContext` (`bot.py`) and built in
`main.py`. `/status` keeps the OpenCode info (active project/session/tasks) and
adds a live system line.

| Module | Responsibility |
| --- | --- |
| `services/system.py` | CPU, RAM, swap, load, uptime, disk, top processes, snapshots |
| `services/network.py` | public IP, interfaces (``ip -j addr``), DNS, gateway |
| `services/vpn.py` | `VpnManager` protocol + `NordVpnProvider` (``nordvpn``) |
| `services/docker.py` | container summary/status; allowlisted restart + logs |
| `services/desktop.py` | grim screenshots, hyprctl windows, screen locking |
| `services/media.py` | mpv playback, camera photos (ffmpeg/v4l2), mic recording (ffmpeg/pulse) |
| `services/stream.py` | `wf-recorder` screen clips streamed to a chat |
| `services/power.py` | reboot/shutdown/suspend guarded by confirmations |
| `services/monitoring.py` | aggregates checks into `/health`; optional proactive loop |
| `core/permissions.py` | READ / CONTROL / DESTRUCTIVE levels + per-user registry |
| `core/confirmation.py` | in-memory pending confirmations with expiry |
| `core/audit.py` | every executed action logged (secrets redacted) |

## Permissions

Three levels: **READ**, **CONTROL**, **DESTRUCTIVE**. Every allowlisted user is
an admin (DESTRUCTIVE) by default. `OTC_READ_ONLY_USER_IDS` downgrades users to
READ. `core/permissions.py::COMMAND_PERMISSIONS` maps each command to the level
it requires; `commands/common.py::check_permission` enforces it per message.

Examples: `/resources` READ · `/vpn` CONTROL · `/docker_restart` CONTROL ·
`/lock` CONTROL · `/reboot` DESTRUCTIVE.

## Confirmations (power)

`/reboot`, `/shutdown` and `/sleep` never execute on the first message. They
create a `PendingConfirmation` (keyed `(user_id, action)`), valid for
`OTC_POWER_CONFIRMATION_TIMEOUT_SECONDS`. The user must reply with
`/confirm_<action>` while it is valid; `/dismiss` cancels everything pending.
`PowerManager.perform` refuses to run without a valid confirmation. Commands
come from a fixed argv (`loginctl reboot|poweroff|suspend`, or an
`OTC_POWER_*_COMMAND` override) — user text never reaches the exec call.

## Audit

`core/audit.py::AuditLogger` writes to the `audit_log` table in the same SQLite
DB: `timestamp`, `user_id`, `action`, `target`, `result`, `error`.
`sanitize_params` redacts/truncates values and any secret-like content so
tokens, credentials or full payloads are never stored. Which actions are
audited: destructive power confirmations (`power.*`), `docker.restart`,
`vpn.connect`, `vpn.connect_dedicated`, `vpn.reconnect`, `desktop.lock`,
`desktop.screenshot`, `media.play_audio`, `media.play_video`, `media.photo`,
`media.record_mic`, `stream.start`, `stream.stop`.

## Command security (concrete)

- **VPN**: `/vpn <country>` is resolved by `VpnManager.resolve_target` against
  the `OTC_VPN_COUNTRIES` allowlist only; anything else → `ValueError` before any
  CLI call. `/vpn_dedicated` only accepts the configured
  `OTC_VPN_DEDICATED_SERVER` (strict charset).
- **Docker**: container names must match `^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$`,
  then must be on `OTC_DOCKER_ALLOWED_CONTAINERS` (if set) and must match a
  container currently known by `docker ps -aq`. Injection like
  `/docker_restart web;rm -rf /` is rejected at validation.
- **Desktop**: `hyprctl clients -j` output is parsed as JSON; window titles are
  truncated for display. Screenshots go to `<data_dir>/screenshots/`.
- **Monitoring**: checks catch per-service exceptions and render clean WARN/ERROR
  lines with no exception text; proactive alerts are only sent on *degradation*
  (blind repeat avoided).

## Media (playback, camera, mic)

- **Playback**: sending an audio file, a **voice note**, or a video file to the
  bot downloads it (bounded by `OTC_MEDIA_MAX_DOWNLOAD_MB`) and starts `mpv`
  through `CommandRunner.spawn` (detached, own process group, no shell). Audio
  runs with `--no-video` (background, no window); video runs `--fullscreen
  --fs-screen=current --ontop --keep-open=no` and closes when it finishes. A
  supervisor task waits up to `OTC_PLAYBACK_MAX_SECONDS`, then kills the process
  group and removes the temporary file.
- **Camera**: `/photo` runs ffmpeg `-f v4l2 -input_format mjpeg -video_size
  <res> -frames:v 1` against `OTC_CAMERA_DEVICE` and replies with the JPEG.
  `OTC_CAMERA_RESOLUTION` configures the resolution.
- **Mic**: `/record_mic [seconds]` records `OTC_MIC_SOURCE` (PulseAudio source
  name or `default`) to MP3 (`libmp3lame`), bounded by `OTC_MIC_DEFAULT_SECONDS`
  (default) and `OTC_MIC_MAX_SECONDS` (hard clamp). Replies with the audio file.

## Live stream (screen clips)

The Telegram Bot API cannot stream in real time, so `/stream` sends a continuous
series of short `wf-recorder` clips (default 5s) as video messages. Each clip is
finalized with SIGINT (graceful muxer close), with a SIGTERM/SIGKILL fallback.
`wf-recorder` must be installed and the service must run inside the same Wayland
session (already the case for the systemd user service). Configuration:
`OTC_STREAM_CLIP_SECONDS`, `OTC_STREAM_FRAMERATE`, `OTC_STREAM_WITH_AUDIO`
(records the default PulseAudio source via `-a`; falls back to video-only if a
clip with audio fails). `/stream_stop` ends the loop; a loop error stops the
stream and notifies the chat. One stream per chat, cleaned up on shutdown.

## Tests

- `tests/test_process.py` — CommandRunner safety (no shell, timeouts, kill).
- `tests/test_permissions.py`, `tests/test_confirmation.py`, `tests/test_audit.py`.
- `tests/test_system.py`, `tests/test_monitoring.py`, `tests/test_network.py`.
- `tests/test_vpn.py`, `tests/test_docker.py`, `tests/test_commands.py`
  (real router dispatch incl. permission, confirmation and injection checks).
- `tests/test_media.py`, `tests/test_stream.py` — playback supervision, camera,
  mic, stream loop (spawn mocked, no real screen recording).