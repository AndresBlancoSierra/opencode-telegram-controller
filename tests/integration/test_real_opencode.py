"""Integration tests that run the real OpenCode binary.

These are excluded by default (``-m not integration``). They spawn actual
``opencode`` subprocesses and may contact model providers, so they are slow and
require the binary to be installed and configured.

Run with:

    uv run pytest -m integration tests/integration/
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opencode_telegram_controller.gitinfo import git_state
from opencode_telegram_controller.main import resolve_opencode_bin
from opencode_telegram_controller.opencode import CLIOpenCodeAdapter
from opencode_telegram_controller.summaries import DeterministicSummaryGenerator

pytestmark = pytest.mark.integration


def skip_unless_opencode():
    binary = resolve_opencode_bin("opencode")
    if binary == "opencode":
        pytest.skip("opencode binary not found")
    return binary


async def test_real_run_and_export(tmp_path):
    binary = skip_unless_opencode()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "README.md").write_text("# Integration test\n")

    adapter = CLIOpenCodeAdapter(binary=binary)
    handle = await adapter.run(
        prompt="What is 2+2? Reply with only the number.",
        cwd=str(repo_dir),
    )

    session_id = None
    saw_start = False
    text_seen = False
    async for event in handle.events():
        if event.session_id:
            session_id = event.session_id
        if event.type == "step_start":
            saw_start = True
        if event.type == "text":
            text_seen = True

    rc = await handle.wait()
    assert rc == 0, await handle.stderr_lines()
    assert saw_start and text_seen
    assert session_id, "no session id captured from the event stream"

    export = await adapter.export(session_id)
    assert isinstance(export, dict)
    assert "messages" in export


async def test_real_executor_end_to_end(tmp_path):
    """Full path: repo -> task -> executor -> summary, with a real model."""
    from opencode_telegram_controller.database import Database
    from opencode_telegram_controller.models import TaskStatus
    from opencode_telegram_controller.projects import ProjectRegistry
    from opencode_telegram_controller.repository import TaskRepository

    binary = skip_unless_opencode()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "a.py").write_text("def add(a, b):\n    return a + b\n")

    db = Database(tmp_path / "tasks.db")
    await db.connect()
    try:
        registry = ProjectRegistry.from_dict(
            {
                "default_project": "T",
                "projects": [{"name": "T", "path": str(repo_dir)}],
            }
        )
        repo = TaskRepository(db)
        task = await repo.create_task(
            user_id=1,
            project_id="T",
            prompt="Fix any issue you see and write tests for add().",
        )

        from opencode_telegram_controller.config import Settings
        from opencode_telegram_controller.task_executor import TaskExecutor

        class SilentNotifier:
            async def send(self, text: str) -> None:
                pass

            async def notify_task_started(self, task, project):
                pass

            async def notify_progress(self, task, snippet):
                pass

            async def notify_task_completed(self, task, summary):
                pass

            async def notify_task_failed(self, task):
                pass

            async def notify_task_cancelled(self, task):
                pass

        settings = Settings(telegram_bot_token="x", allowed_user_ids=[1])
        executor = TaskExecutor(
            adapter=CLIOpenCodeAdapter(binary=binary),
            repo=repo,
            registry=registry,
            notifier=SilentNotifier(),
            summary_generator=DeterministicSummaryGenerator(),
            settings=settings,
        )
        await executor.execute(task.id)

        stored = await repo.get_task(task.id)
        assert stored.status == TaskStatus.COMPLETED, stored.error
        assert stored.session_id
        assert stored.summary
        assert any("a.py" in line for line in (stored.summary or "").splitlines())
    finally:
        await db.close()


async def test_real_git_state_and_summary(tmp_path):
    """Verify git state detection and deterministic summary on a real repo."""
    import subprocess

    skip_unless_opencode()

    from opencode_telegram_controller.models import Task, TaskStatus

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo_dir), *a], capture_output=True, text=True, check=True
    )
    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo_dir / "x.txt").write_text("hello\n")
    git("add", ".")
    git("commit", "-m", "initial")

    before = await git_state(repo_dir)
    assert before.is_repo and before.branch == "main"

    (repo_dir / "y.txt").write_text("new\n")
    after = await git_state(repo_dir)
    assert after.is_dirty

    now = datetime.now(UTC)

    task = Task(
        id=1,
        user_id=1,
        project_id="T",
        prompt="test",
        status=TaskStatus.COMPLETED,
        created_at=now,
        started_at=now,
        finished_at=now,
        exit_code=0,
        session_id="ses_1",
    )
    summary = await DeterministicSummaryGenerator().generate(
        task=task,
        export={
            "info": {"model": {"providerID": "opencode", "id": "test"}},
            "messages": [
                {
                    "info": {"role": "assistant"},
                    "parts": [{"type": "text", "text": "done."}],
                }
            ],
        },
        git_before=before,
        git_after=after,
        log_tail="1 passed",
    )
    assert "y.txt" in summary
    assert "done." in summary
