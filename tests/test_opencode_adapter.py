"""Tests for the OpenCode CLI adapter and event parsing."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from opencode_telegram_controller.opencode import CLIOpenCodeAdapter
from opencode_telegram_controller.opencode.events import is_text_event, parse_line


async def test_parse_valid_event():
    event = parse_line('{"type":"text","sessionID":"ses_1","part":{"type":"text","text":"hi"}}')
    assert event is not None
    assert event.type == "text"
    assert event.session_id == "ses_1"
    assert event.text == "hi"
    assert is_text_event(event)


async def test_parse_empty_and_garbage():
    assert parse_line("") is None
    assert parse_line("   \n") is None
    assert parse_line("not json") is None
    assert parse_line('"just a string"') is None


async def test_parse_missing_part():
    event = parse_line('{"type":"step_start","sessionID":"ses_2"}')
    assert event is not None
    assert event.type == "step_start"
    assert event.part_type is None


async def test_parse_tool_use():
    line = json.dumps(
        {
            "type": "tool_use",
            "sessionID": "ses_3",
            "part": {"type": "tool", "name": "bash", "state": {"status": "running"}},
        }
    )
    event = parse_line(line)
    assert event is not None
    assert event.part_type == "tool"
    assert not is_text_event(event)


def make_fake_subprocess(stdout_lines=(), stderr_lines=(), returncode=0):
    async def read_bytes(lines):
        for line in lines:
            yield line.encode()

    process = SimpleNamespace(
        pid=4242,
        returncode=returncode,
        stdout=read_bytes(stdout_lines),
        stderr=read_bytes(stderr_lines),
    )

    async def fake_wait():
        return returncode

    async def fake_communicate():
        return ("\n".join(stdout_lines).encode(), "\n".join(stderr_lines).encode())

    process.wait = fake_wait
    process.communicate = fake_communicate
    return process


async def test_cli_run_command_shape(monkeypatch):
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        captured["start_new_session"] = kwargs.get("start_new_session")
        return make_fake_subprocess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = CLIOpenCodeAdapter(binary="opencode", model="m", agent="build")
    handle = await adapter.run(prompt="fix $PWD && rm -rf /", cwd="/tmp/x")
    assert captured["args"][:5] == ("opencode", "run", "fix $PWD && rm -rf /", "--format", "json")
    assert "--dir" in captured["args"] and "/tmp/x" in captured["args"]
    assert "-m" in captured["args"] and "m" in captured["args"]
    assert "--agent" in captured["args"] and "build" in captured["args"]
    assert "--title" in captured["args"]
    assert captured["start_new_session"] is True
    assert captured["cwd"] == "/tmp/x"
    assert handle.process.pid == 4242


async def test_cli_run_continues_session(monkeypatch):
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        return make_fake_subprocess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = CLIOpenCodeAdapter(binary="opencode")
    await adapter.run(prompt="continue", cwd="/tmp/x", session_id="ses_abc")
    assert "-s" in captured["args"]
    assert "ses_abc" in captured["args"]


async def test_cli_env_strips_inherited_vars(monkeypatch):
    captured = {}
    import os

    monkeypatch.setenv("OPENCODE", "server")
    monkeypatch.setenv("OPENCODE_PID", "123")
    monkeypatch.setenv("KEEP_ME", "1")

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return make_fake_subprocess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = CLIOpenCodeAdapter(binary="opencode")
    await adapter.run(prompt="x", cwd="/tmp/x")
    assert "OPENCODE" not in captured["env"]
    assert "OPENCODE_PID" not in captured["env"]
    assert captured["env"]["KEEP_ME"] == "1"
    assert os.environ.get("OPENCODE") == "server"


async def test_cli_events_stream(monkeypatch):
    lines = [
        json.dumps({"type": "step_start", "sessionID": "ses_1"}),
        json.dumps({"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "hi"}}),
        "not-json",
        "",
    ]
    proc = make_fake_subprocess(stdout_lines=lines)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = CLIOpenCodeAdapter(binary="opencode")
    handle = await adapter.run(prompt="x", cwd="/tmp/x")
    events = [e async for e in handle.events()]
    assert len(events) == 2
    assert events[0].type == "step_start"
    assert events[1].text == "hi"


async def test_cli_stderr_collected(monkeypatch):
    proc = make_fake_subprocess(stderr_lines=["warn line 1", "error line 2"])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = CLIOpenCodeAdapter(binary="opencode")
    handle = await adapter.run(prompt="x", cwd="/tmp/x")
    lines = await handle.stderr_lines()
    assert lines == ["warn line 1", "error line 2"]


async def test_cli_export_success(monkeypatch):
    data = {"info": {"model": {"id": "m"}}}
    proc = make_fake_subprocess(stdout_lines=[json.dumps(data)], returncode=0)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = CLIOpenCodeAdapter(binary="opencode")
    result = await adapter.export("ses_1")
    assert result == data


async def test_cli_export_nonzero_returns_empty(monkeypatch):
    proc = make_fake_subprocess(returncode=1)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = CLIOpenCodeAdapter(binary="opencode")
    assert await adapter.export("ses_1") == {}


async def test_cli_export_invalid_json_returns_empty(monkeypatch):
    proc = make_fake_subprocess(stdout_lines=["not json"], returncode=0)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = CLIOpenCodeAdapter(binary="opencode")
    assert await adapter.export("ses_1") == {}
