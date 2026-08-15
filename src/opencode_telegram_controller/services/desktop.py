"""Desktop capability: screenshots, visible windows and screen locking.

Backends are detected at runtime (grim/slurp for Wayland, hyprctl + hyprlock
for Hyprland). Screenshots are captured to a temporary PNG file that the
handler reads and sends to Telegram.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..core.process import CommandRunner
from .base import ServiceUnavailableError


@dataclass
class ScreenshotResult:
    path: Path
    size_bytes: int


@dataclass
class WindowInfo:
    title: str
    class_name: str | None = None
    workspace: int | None = None
    pid: int | None = None


class DesktopManager:
    """Pay attention: exposes capture, window listing and session locking."""

    def __init__(self, *, settings: Settings, runner: CommandRunner) -> None:
        self.settings = settings
        self.runner = runner
        self._screenshot_dir = settings.data_dir / "screenshots"

    def screenshot_backend(self) -> str | None:
        """Return the available screenshot backend name or None."""
        if not self.settings.screenshot_enabled:
            return None
        if self.runner.can_run("grim"):
            return "grim"
        return None

    def hyprland_available(self) -> bool:
        return self.runner.can_run("hyprctl")

    def locker_command(self) -> str:
        for candidate in ("hyprlock", "swaylock", "waylock"):
            if self.runner.can_run(candidate):
                return candidate
        return ""

    async def screenshot(self) -> ScreenshotResult:
        backend = self.screenshot_backend()
        if backend is None:
            if not self.settings.screenshot_enabled:
                raise ServiceUnavailableError("Screenshot", "Disabled by configuration")
            raise ServiceUnavailableError(
                "Screenshot", "Required screenshot backend is not installed"
            )
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(
            prefix="screenshot-", suffix=".png", dir=self._screenshot_dir
        )
        os.close(fd)
        path = Path(raw_path)
        if backend == "grim":
            await self.runner.run(("grim", str(path)), timeout=self.settings.timeout_quick_seconds)
        result = ScreenshotResult(path=path, size_bytes=path.stat().st_size)
        if result.size_bytes == 0:
            path.unlink(missing_ok=True)
            raise ServiceUnavailableError("Screenshot", "Capture produced an empty image")
        return result

    async def windows(self, limit: int = 20) -> list[WindowInfo]:
        if not self.hyprland_available():
            raise ServiceUnavailableError("Windows", "Hyprland (hyprctl) is not available")
        result = await self.runner.run(
            ("hyprctl", "clients", "-j"), timeout=self.settings.timeout_quick_seconds
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise ServiceUnavailableError("Windows", "hyprctl returned invalid JSON") from None
        if not isinstance(payload, list):
            raise ServiceUnavailableError("Windows", "unexpected hyprctl output")
        windows_list: list[WindowInfo] = []
        for item in payload:
            workspace = item.get("workspace")
            workspace_id = workspace.get("id") if isinstance(workspace, dict) else None
            windows_list.append(
                WindowInfo(
                    title=(item.get("title") or "").strip() or "(untitled)",
                    class_name=item.get("class"),
                    workspace=workspace_id,
                    pid=item.get("pid"),
                )
            )
        windows_list.sort(
            key=lambda w: (
                w.workspace if w.workspace is not None else 10**9,
                w.title.lower(),
            )
        )
        return windows_list[:limit]

    async def lock(self) -> None:
        command = self.locker_command()
        if not command:
            raise ServiceUnavailableError("Lock", "No screen locker (hyprlock) is installed")
        await self.runner.run((command,), timeout=self.settings.timeout_quick_seconds)
