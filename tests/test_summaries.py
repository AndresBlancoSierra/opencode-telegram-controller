"""Tests for the deterministic summary generator."""

from __future__ import annotations

from opencode_telegram_controller.models import GitState, Task, TaskStatus
from opencode_telegram_controller.summaries import DeterministicSummaryGenerator

GEN = DeterministicSummaryGenerator()


def make_task(**overrides) -> Task:
    from datetime import UTC, datetime

    defaults = dict(
        id=1,
        user_id=1,
        project_id="A",
        prompt="run the tests",
        status=TaskStatus.COMPLETED,
        created_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        started_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, 0, 3, tzinfo=UTC),
        exit_code=0,
    )
    defaults.update(overrides)
    return Task(**defaults)


def sample_export(**overrides) -> dict:
    export = {
        "info": {
            "model": {"providerID": "opencode", "id": "big-pickle"},
            "tokens": {"input": 1000, "output": 500},
            "cost": 0.0123,
        },
        "messages": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "run the tests"}]},
            {
                "info": {"role": "assistant", "finish": "stop"},
                "parts": [
                    {"type": "text", "text": "Fixed the failing test."},
                    {"type": "tool", "name": "bash"},
                ],
            },
        ],
    }
    export.update(overrides)
    return export


async def test_summary_includes_metadata():
    export = sample_export()
    summary = await GEN.generate(
        task=make_task(),
        export=export,
        git_before=GitState(),
        git_after=GitState(),
        log_tail="",
    )
    assert "2m 0s" in summary or "Duration" in summary
    assert "opencode/big-pickle" in summary
    assert "1,000" in summary and "500" in summary
    assert "$0.0123" in summary


async def test_summary_includes_assistant_text():
    summary = await GEN.generate(
        task=make_task(),
        export=sample_export(),
        git_before=GitState(),
        git_after=GitState(),
        log_tail="",
    )
    assert "Fixed the failing test." in summary
    assert "Summary:" in summary


async def test_summary_only_uses_last_assistant_message():
    """A continued session's export holds earlier turns; the tail must not
    repeat the previous assistant answer."""
    export = {
        "info": {"model": {"providerID": "opencode", "id": "m"}},
        "messages": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "old prompt"}]},
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "text", "text": "OLD long previous answer that must not repeat."}
                ],
            },
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "new prompt"}]},
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "New actual answer."}],
            },
        ],
    }
    summary = await GEN.generate(
        task=make_task(),
        export=export,
        git_before=GitState(),
        git_after=GitState(),
        log_tail="",
    )
    assert "New actual answer." in summary
    assert "must not repeat" not in summary


async def test_summary_detects_tests_from_log():
    summary = await GEN.generate(
        task=make_task(),
        export=sample_export(),
        git_before=GitState(),
        git_after=GitState(),
        log_tail="1 failed, 12 passed in 0.42s",
    )
    assert "Tests:" in summary
    assert "1" in summary.split("Tests:")[1]
    assert "12" in summary.split("Tests:")[1]


async def test_summary_lists_changed_files():
    after = GitState(
        is_repo=True, branch="main", head="def5678", short_status=[" M src/a.py", "?? b.py"]
    )
    before = GitState(is_repo=True, branch="main", head="abc1234")
    summary = await GEN.generate(
        task=make_task(), export=sample_export(), git_before=before, git_after=after, log_tail=""
    )
    assert "src/a.py" in summary
    assert "b.py" in summary
    assert "Commit: def5678" in summary


async def test_summary_warns_uncommitted():
    after = GitState(is_repo=True, branch="main", short_status=[" M a.py"])
    summary = await GEN.generate(
        task=make_task(),
        export=sample_export(),
        git_before=GitState(),
        git_after=after,
        log_tail="",
    )
    assert "Uncommitted" in summary


async def test_summary_handles_empty_export():
    summary = await GEN.generate(
        task=make_task(),
        export={},
        git_before=GitState(),
        git_after=GitState(),
        log_tail="",
    )
    assert summary


async def test_summary_no_metadata_note():
    task = make_task(started_at=None, finished_at=None, exit_code=None)
    summary = await GEN.generate(
        task=task, export={}, git_before=GitState(), git_after=GitState(), log_tail=""
    )
    assert "No additional details" in summary


async def test_detect_tool_names():
    export = sample_export()
    from opencode_telegram_controller.summaries.deterministic import _collect_tool_names

    names = _collect_tool_names(export)
    assert "bash" in names


async def test_model_from_export_string():
    from opencode_telegram_controller.summaries.deterministic import _model_from_export

    assert _model_from_export({"info": {"model": "gpt-4o"}}) == "gpt-4o"
    assert _model_from_export({}) is None
