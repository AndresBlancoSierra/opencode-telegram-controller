"""Tests for DockerManager: parsing, summaries, allowlist and injection safety."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencode_telegram_controller.services.docker import DockerError, DockerManager

NAMES_PS = "web\napi\ndb\n"


def make_manager(monkeypatch, *, script=None, allowlist=(), log_lines=200):
    from opencode_telegram_controller.config import Settings
    from opencode_telegram_controller.core.process import CommandRunner

    settings = Settings(
        telegram_bot_token="x",
        allowed_user_ids=[1],
        docker_allowed_containers=list(allowlist),
        docker_logs_lines=log_lines,
    )
    manager = DockerManager(settings=settings, runner=CommandRunner())
    calls = []

    async def fake_run(args, **kwargs):
        calls.append(args)
        if script is None:
            raise AssertionError(f"unscripted run {args!r}")
        sub = args[1] if len(args) > 1 else args[0]
        step = script.get(args) or script.get(sub)
        if step is None:
            raise AssertionError(f"unscripted run {args!r}")
        if callable(step):
            return step(args, kwargs)
        returncode = step.get("returncode", 0)
        return SimpleNamespace(
            returncode=returncode,
            ok=returncode == 0,
            stdout=step.get("stdout", ""),
            stderr=step.get("stderr", ""),
        )

    manager.runner.run = fake_run  # type: ignore[method-assign]
    return manager, calls


def test_validate_name_accepts_normal():
    manager, _ = make_manager(None)
    assert manager.validate_name(" web-1 ") == "web-1"


@pytest.mark.parametrize(
    "name",
    [
        "web;rm -rf /",
        "web$(whoami)",
        "--privileged",
        "../..",
        "",
        "sh -c evil",
        "x/../y",
    ],
)
def test_validate_name_rejects_injection(name):
    manager, _ = make_manager(None)
    with pytest.raises(DockerError):
        manager.validate_name(name)


async def test_containers_parses_ps_output(monkeypatch):
    manager, calls = make_manager(
        monkeypatch,
        script={
            "ps": {
                "stdout": (
                    "abc123\tweb\tnginx:1.25\tUp 3 hours (healthy)\trunning\n"
                    "def456\tdb\tpostgres:16\tUp 3 hours (unhealthy)\trunning\n"
                    "ghi789\told\talpine\tExited (0) 2 days ago\texited\n"
                )
            },
        },
    )
    containers = await manager.containers()
    assert [c.name for c in containers] == ["web", "db", "old"]
    assert containers[0].state == "running" and containers[0].health == "healthy"
    assert containers[1].health == "unhealthy"
    assert containers[2].state == "exited" and containers[2].health is None
    assert calls[0][1] == "ps"


async def test_summary_counts(monkeypatch):
    manager, calls = make_manager(
        monkeypatch,
        script={
            "ps": {
                "stdout": (
                    "a\tweb\tnginx\tUp (healthy)\trunning\n"
                    "b\tdb\tpostgres\tUp (unhealthy)\trunning\n"
                    "c\told\talpine\tExited\texited\n"
                )
            },
        },
    )
    summary = await manager.summary()
    assert summary.total == 3
    assert summary.running == 2
    assert summary.stopped == 1
    assert summary.unhealthy == 1


async def test_restart_allowlisted_container(monkeypatch):
    manager, calls = make_manager(
        monkeypatch,
        script={
            ("docker", "ps", "-aq", "--format", "{{.Names}}"): {"stdout": NAMES_PS},
            "restart": {"stdout": "web\n"},
        },
        allowlist=["web"],
    )
    await manager.restart("web")
    assert ("docker", "restart", "web") in calls


async def test_restart_rejects_non_allowlisted(monkeypatch):
    manager, calls = make_manager(
        monkeypatch,
        script={("docker", "ps", "-aq", "--format", "{{.Names}}"): {"stdout": NAMES_PS}},
        allowlist=["web"],
    )
    with pytest.raises(DockerError, match="allowlist"):
        await manager.restart("db")
    assert not any(args[1] == "restart" for args in calls)


async def test_restart_rejects_unknown_container_without_allowlist(monkeypatch):
    manager, calls = make_manager(
        monkeypatch,
        script={("docker", "ps", "-aq", "--format", "{{.Names}}"): {"stdout": NAMES_PS}},
    )
    with pytest.raises(DockerError, match="does not exist"):
        await manager.restart("ghost")
    assert not any(args[1] == "restart" for args in calls)


async def test_logs_clamps_and_truncates(monkeypatch):
    manager, calls = make_manager(
        monkeypatch,
        script={
            ("docker", "ps", "-aq", "--format", "{{.Names}}"): {"stdout": NAMES_PS},
            "logs": {"stdout": "line1\nline2\n" * 2000},
        },
        log_lines=300,
    )
    logs = await manager.logs("web", lines=9999)
    assert logs.startswith("line1") and logs.endswith("line2\n")
    assert len(logs) <= 6000
    assert calls[-1] == ("docker", "logs", "--tail", "300", "web")


async def test_logs_failure_raises(monkeypatch):
    manager, calls = make_manager(
        monkeypatch,
        script={
            ("docker", "ps", "-aq", "--format", "{{.Names}}"): {"stdout": NAMES_PS},
            "logs": {"returncode": 1, "stderr": "Error: no such container\n"},
        },
    )
    with pytest.raises(DockerError, match="no such container"):
        await manager.logs("web")
