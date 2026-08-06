"""Project allowlist management.

Only projects configured in the YAML allowlist can be used as OpenCode
workspaces. The bot never runs tasks against arbitrary filesystem paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Project


class ProjectConfigError(Exception):
    """Raised when the projects configuration file is invalid."""


@dataclass
class ProjectRegistry:
    projects: dict[str, Project] = field(default_factory=dict)
    default_project: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> ProjectRegistry:
        default = data.get("default_project")
        raw_projects = data.get("projects") or []
        registry = cls(default_project=default)
        seen: set[str] = set()
        for item in raw_projects:
            if not isinstance(item, dict):
                raise ProjectConfigError(
                    f"Invalid project entry (expected a mapping, got {type(item).__name__})"
                )
            name = str(item.get("name", "")).strip()
            if not name:
                raise ProjectConfigError("Project entry is missing 'name'")
            if name in seen:
                raise ProjectConfigError(f"Duplicate project name: {name!r}")
            seen.add(name)
            raw_path = item.get("path")
            if not raw_path:
                raise ProjectConfigError(f"Project {name!r} is missing 'path'")
            path = Path(str(raw_path)).expanduser().resolve()
            if not path.is_dir():
                raise ProjectConfigError(
                    f"Project {name!r} path does not exist or is not a directory: {path}"
                )
            registry.projects[name] = Project(
                name=name,
                path=path,
                description=str(item.get("description", "")).strip(),
                enabled=bool(item.get("enabled", True)),
            )
        return registry

    @classmethod
    def from_file(cls, path: Path) -> ProjectRegistry:
        path = Path(path)
        if not path.exists():
            raise ProjectConfigError(f"Projects file not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ProjectConfigError(f"Invalid projects file (expected a mapping): {path}")
        return cls.from_dict(data)

    def get(self, name: str) -> Project | None:
        return self.projects.get(name)

    def enabled_projects(self) -> list[Project]:
        return [p for p in self.projects.values() if p.enabled]

    def enabled_names(self) -> list[str]:
        return [p.name for p in self.enabled_projects()]

    def resolve(self, name: str) -> Project:
        """Return the project or raise KeyError when missing or disabled."""
        project = self.get(name)
        if project is None:
            raise KeyError(name)
        if not project.enabled:
            raise KeyError(f"{name} is disabled")
        return project
