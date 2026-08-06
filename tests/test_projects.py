"""Tests for the project allowlist."""

from __future__ import annotations

import pytest

from opencode_telegram_controller.projects import ProjectConfigError, ProjectRegistry


def test_registry_from_dict(tmp_path):
    path = tmp_path / "x"
    path.mkdir()
    data = {
        "default_project": "One",
        "projects": [{"name": "One", "path": str(path), "description": "d"}],
    }
    registry = ProjectRegistry.from_dict(data)
    assert registry.default_project == "One"
    project = registry.get("One")
    assert project is not None
    assert project.enabled is True
    assert project.path == path.resolve()


def test_registry_expands_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    proj = home / "code"
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    data = {"projects": [{"name": "X", "path": "~/code"}]}
    registry = ProjectRegistry.from_dict(data)
    assert str(registry.get("X").path).startswith(str(home))


def test_missing_path_raises(tmp_path):
    data = {"projects": [{"name": "X", "path": str(tmp_path / "nope")}]}
    with pytest.raises(ProjectConfigError, match="does not exist"):
        ProjectRegistry.from_dict(data)


def test_duplicate_name_raises(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    data = {
        "projects": [
            {"name": "X", "path": str(a)},
            {"name": "X", "path": str(b)},
        ]
    }
    with pytest.raises(ProjectConfigError, match="Duplicate"):
        ProjectRegistry.from_dict(data)


def test_missing_name_raises(tmp_path):
    p = tmp_path / "p"
    p.mkdir()
    with pytest.raises(ProjectConfigError, match="name"):
        ProjectRegistry.from_dict({"projects": [{"path": str(p)}]})


def test_disabled_project_resolve_raises(tmp_path):
    p = tmp_path / "p"
    p.mkdir()
    data = {"projects": [{"name": "X", "path": str(p), "enabled": False}]}
    registry = ProjectRegistry.from_dict(data)
    assert registry.get("X").enabled is False
    assert registry.enabled_names() == []
    with pytest.raises(KeyError):
        registry.resolve("X")


def test_enabled_projects_only(tmp_path):
    p = tmp_path / "p"
    q = tmp_path / "q"
    p.mkdir()
    q.mkdir()
    data = {
        "projects": [
            {"name": "A", "path": str(p), "enabled": True},
            {"name": "B", "path": str(q), "enabled": False},
        ]
    }
    registry = ProjectRegistry.from_dict(data)
    assert registry.enabled_names() == ["A"]
