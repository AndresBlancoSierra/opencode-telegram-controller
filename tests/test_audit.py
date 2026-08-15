"""Tests for the audit logger and parameter sanitization."""

from __future__ import annotations

from opencode_telegram_controller.core.audit import AuditLogger, sanitize_params


async def test_record_and_list(db):
    logger = AuditLogger(db)
    await logger.record(user_id=123, action="vpn.connect", target="us", result="success")
    await logger.record(
        user_id=123,
        action="docker.restart",
        target="immich",
        result="failed",
        error="no such container",
    )

    entries = await logger.list_recent()
    assert len(entries) == 2
    latest = entries[0]
    assert latest.action == "docker.restart"
    assert latest.target == "immich"
    assert latest.result == "failed"
    assert latest.error == "no such container"
    assert latest.user_id == 123


async def test_sanitize_params_redacts_secrets():
    params = {"server": "us1234", "password": "hunter2", "API_TOKEN": "abc", "country": "us"}
    cleaned = sanitize_params(params)
    assert "us1234" in cleaned
    assert "country" in cleaned
    assert "hunter2" not in cleaned
    assert "abc" not in cleaned
    assert "[REDACTED]" in cleaned


async def test_sanitize_params_truncates_long_values():
    params = {"prompt": "x" * 500}
    cleaned = sanitize_params(params)
    assert len(cleaned) < 400
    assert "..." in cleaned


async def test_sanitize_params_empty():
    assert sanitize_params(None) is None
    assert sanitize_params({}) is None


async def test_list_recent_respects_limit(db):
    logger = AuditLogger(db)
    for i in range(5):
        await logger.record(user_id=123, action=f"test.{i}", result="success")
    entries = await logger.list_recent(limit=2)
    assert len(entries) == 2
    assert entries[0].action == "test.4"
