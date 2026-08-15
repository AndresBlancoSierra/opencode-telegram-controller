"""Docker capability: containers summary, status, logs and restarts.

Container names are always validated: first against a strict character set,
then against the set of containers currently known to Docker, and finally
(if configured) against ``OTC_DOCKER_ALLOWED_CONTAINERS``. The name is passed
as a single fixed argument to the ``docker`` CLI — never through a shell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Settings
from ..core.process import CommandRunner

_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")


class DockerError(Exception):
    """Raised for validated-but-failed Docker operations."""


def _docker_error(result, fallback: str) -> str:
    """Build a human message from ``docker`` output (resists empty stderr)."""
    stderr_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    if stderr_lines:
        return f"{fallback}: {stderr_lines[-1]}"
    return fallback


_HEALTH_PATTERN = re.compile(r"\((healthy|unhealthy|health: [a-z]+)\)")


def _extract_health(status: str) -> str | None:
    """Pull the health state out of a ``docker ps`` status string, if present."""
    match = _HEALTH_PATTERN.search(status)
    if not match:
        return None
    value = match.group(1)
    if value.startswith("health: "):
        return value[len("health: ") :]
    return value


@dataclass
class DockerSummary:
    total: int
    running: int
    stopped: int
    unhealthy: int

    @property
    def healthy_text(self) -> str:
        if not self.total:
            return "no containers"
        return f"{self.running}/{self.total} running"


@dataclass
class DockerContainer:
    id: str
    name: str
    image: str
    status: str  # human status from docker, e.g. "Up 3 hours"
    state: str  # running | exited | ...
    health: str | None = None

    @property
    def healthy(self) -> bool:
        return not (self.state == "exited" or self.health == "unhealthy")


@dataclass
class DockerStatus:
    containers: list[DockerContainer] = field(default_factory=list)


class DockerManager:
    """Inspect and (allowlisted) restart Docker containers."""

    def __init__(self, *, settings: Settings, runner: CommandRunner) -> None:
        self.settings = settings
        self.runner = runner

    def available(self) -> bool:
        return self.runner.can_run("docker")

    def is_allowed(self, name: str) -> bool:
        allowlist = self.settings.docker_allowed_containers
        return not allowlist or name in allowlist

    def validate_name(self, name: str) -> str:
        stripped = name.strip()
        if not stripped or not _NAME_PATTERN.fullmatch(stripped):
            raise DockerError(f"Invalid container name: {name!r}")
        return stripped

    async def _known_containers(self) -> set[str]:
        result = await self.runner.run(("docker", "ps", "-aq", "--format", "{{.Names}}"))
        return {line for line in result.stdout.splitlines() if line and not line.isspace()}

    async def summary(self) -> DockerSummary:
        containers = await self.containers()
        return DockerSummary(
            total=len(containers),
            running=sum(1 for c in containers if c.state == "running"),
            stopped=sum(1 for c in containers if c.state != "running"),
            unhealthy=sum(1 for c in containers if c.health == "unhealthy"),
        )

    async def containers(self) -> list[DockerContainer]:
        result = await self.runner.run(
            (
                "docker",
                "ps",
                "-a",
                "--format",
                "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.State}}",
            ),
            timeout=self.settings.timeout_docker_seconds,
        )
        items: list[DockerContainer] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            status = parts[3].strip()
            items.append(
                DockerContainer(
                    id=parts[0].strip(),
                    name=parts[1].strip(),
                    image=parts[2].strip(),
                    status=status,
                    state=parts[4].strip(),
                    health=_extract_health(status),
                )
            )
        return items

    async def _require_operable_container(self, name: str) -> str:
        stripped = self.validate_name(name)
        if not self.is_allowed(stripped):
            raise DockerError(
                f"Container {stripped!r} is not on the allowlist (OTC_DOCKER_ALLOWED_CONTAINERS)"
            )
        known = await self._known_containers()
        if stripped not in known:
            raise DockerError(f"Container {stripped!r} does not exist (or is not permitted)")
        return stripped

    async def restart(self, name: str) -> None:
        container = await self._require_operable_container(name)
        result = await self.runner.run(
            ("docker", "restart", container),
            timeout=self.settings.timeout_docker_seconds,
        )
        if not result.ok:
            raise DockerError(_docker_error(result, f"Failed to restart {container!r}"))

    async def logs(self, name: str, lines: int = 200) -> str:
        container = await self._require_operable_container(name)
        lines = max(1, min(int(lines), self.settings.docker_logs_lines))
        result = await self.runner.run(
            ("docker", "logs", "--tail", str(lines), container),
            timeout=self.settings.timeout_docker_seconds,
            check=False,
        )
        if not result.ok:
            raise DockerError(_docker_error(result, f"Failed to read logs of {container!r}"))
        return (result.stdout + result.stderr)[-6000:]
