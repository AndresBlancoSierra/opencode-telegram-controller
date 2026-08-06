"""Executor-level success and failure tests."""

from __future__ import annotations

from conftest import event, make_executor, make_settings

from opencode_telegram_controller.models import TaskStatus


def build(adapter, repo, registry, notifier, settings=None):
    settings = settings or make_settings(default_timeout_seconds=30)
    return make_executor(adapter, repo, registry, notifier, settings)


async def test_success_completes_and_summarizes(repo, registry, notifier, adapter):
    events = [
        event("step_start", session_id="ses_1"),
        event("text", text="Let me fix that"),
        event("step_finish", session_id="ses_1"),
    ]
    adapter.queue_run(events=events, exit_code=0)
    adapter.exports["ses_1"] = {
        "info": {"model": {"id": "big-pickle"}, "tokens": {"input": 10, "output": 5}},
        "messages": [],
    }
    executor = build(adapter, repo, registry, notifier)
    task = await repo.create_task(user_id=1, project_id="A", prompt="fix the bug")
    await executor.execute(task.id)
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.COMPLETED
    assert stored.exit_code == 0
    assert stored.session_id == "ses_1"
    assert stored.summary is not None
    assert notifier.completed == [task.id]
    assert notifier.started == [task.id]
    assert adapter.runs[0]["cwd"] == registry.get("A").path.as_posix()
    assert adapter.runs[0]["prompt"] == "fix the bug"


async def test_failure_nonzero_exit(repo, registry, notifier, adapter):
    adapter.queue_run(
        events=[event("text", text="oops")], exit_code=2, stderr=["line1", "line2", "boom!"]
    )
    executor = build(adapter, repo, registry, notifier)
    task = await repo.create_task(user_id=1, project_id="A", prompt="x")
    await executor.execute(task.id)
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.FAILED
    assert stored.exit_code == 2
    assert "code 2" in stored.error
    assert "boom!" in stored.error
    assert "oops" in stored.log_tail
    assert notifier.failed == [task.id]


async def test_failure_adapter_error(repo, registry, notifier, adapter):
    adapter.run_error = RuntimeError("opencode binary not found")
    executor = build(adapter, repo, registry, notifier)
    task = await repo.create_task(user_id=1, project_id="A", prompt="x")
    await executor.execute(task.id)
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.FAILED
    assert "not found" in stored.error


async def test_failure_timeout(repo, registry, notifier, adapter):
    settings = make_settings(default_timeout_seconds=1)
    handle = adapter.queue_run(exit_code=0, wait_delay=5.0)
    executor = build(adapter, repo, registry, notifier, settings)
    task = await repo.create_task(user_id=1, project_id="A", prompt="x")
    await executor.execute(task.id)
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.FAILED
    assert "Timed out" in stored.error
    assert handle.cancelled is True


async def test_missing_project_fails(repo, registry, notifier, adapter):
    executor = build(adapter, repo, registry, notifier)
    task = await repo.create_task(user_id=1, project_id="A", prompt="x")
    await repo.conn.execute("UPDATE tasks SET project_id = 'Gone' WHERE id = ?", (task.id,))
    await repo.conn.commit()
    await executor.execute(task.id)
    stored = await repo.get_task(task.id)
    assert stored.status == TaskStatus.FAILED
    assert "Project" in stored.error


async def test_missing_task_is_noop(repo, registry, notifier, adapter):
    executor = build(adapter, repo, registry, notifier)
    await executor.execute(999999)
    assert adapter.runs == []


async def test_session_id_persisted_during_run(repo, registry, notifier, adapter):
    adapter.queue_run(
        events=[event("step_start", session_id="ses_new")], exit_code=0, wait_delay=0.2
    )
    executor = build(adapter, repo, registry, notifier)
    task = await repo.create_task(user_id=1, project_id="A", prompt="x")
    await repo.set_session_id(task.id, "ses_old")
    await executor.execute(task.id)
    stored = await repo.get_task(task.id)
    assert stored.session_id == "ses_new"
