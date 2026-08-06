"""Deterministic summary generator.

Builds a summary from structured task data only: the OpenCode session export,
Git state before/after the task, the task log tail and task metadata.

The system remains fully functional without any LLM. Test result detection is a
best-effort heuristic over the session text and logs.
"""

from __future__ import annotations

import re

from ..formatting import format_duration
from ..models import GitState, Task
from .base import SummaryGenerator

_ASSISTANT_TEXT_LIMIT = 800
_FILES_LIMIT = 25

_PASSED_RE = re.compile(r"(\d+)\s+passed")
_FAILED_RE = re.compile(r"(\d+)\s+failed")
_ERRORS_RE = re.compile(r"(\d+)\s+error")
_WARNING_RE = re.compile(r"\bwarning\b", re.IGNORECASE)


def _collect_text(export: dict) -> str:
    parts: list[str] = []
    for message in export.get("messages", []):
        info = message.get("info", {}) if isinstance(message, dict) else {}
        if info.get("role") != "assistant":
            continue
        for part in message.get("parts", []):
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text", "")).strip()
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _collect_tool_names(export: dict) -> list[str]:
    names: list[str] = []
    for message in export.get("messages", []):
        if not isinstance(message, dict):
            continue
        for part in message.get("parts", []):
            if isinstance(part, dict) and part.get("type") == "tool":
                name = str(part.get("name", "")).strip()
                if name and name not in names:
                    names.append(name)
    return names


def _last_finish(export: dict) -> str | None:
    for message in reversed(export.get("messages", [])):
        if isinstance(message, dict):
            finish = message.get("info", {}).get("finish")
            if finish:
                return str(finish)
    return None


def _token_cost(export: dict) -> str:
    info = export.get("info", {}) if isinstance(export.get("info"), dict) else {}
    tokens = info.get("tokens") or {}
    input_tokens = tokens.get("input")
    output_tokens = tokens.get("output")
    cost = info.get("cost")
    bits: list[str] = []
    if isinstance(input_tokens, (int, float)):
        bits.append(f"in {int(input_tokens):,}")
    if isinstance(output_tokens, (int, float)):
        bits.append(f"out {int(output_tokens):,}")
    if isinstance(cost, (int, float)) and cost > 0:
        bits.append(f"${cost:.4f}")
    if bits:
        return " • " + " ".join(bits)
    return ""


def _detect_tests(text: str) -> list[str]:
    lines: list[str] = []
    for pattern in (_PASSED_RE, _FAILED_RE, _ERRORS_RE):
        matches = pattern.findall(text)
        for value in matches:
            if value not in lines:
                lines.append(value)
    if not lines:
        return []
    return lines


def _assistant_tail(export: dict) -> str:
    text = _collect_text(export).strip()
    if not text:
        return ""
    if len(text) <= _ASSISTANT_TEXT_LIMIT:
        return text
    return "…" + text[-_ASSISTANT_TEXT_LIMIT:].strip()


class DeterministicSummaryGenerator(SummaryGenerator):
    async def generate(
        self,
        *,
        task: Task,
        export: dict,
        git_before: GitState,
        git_after: GitState,
        log_tail: str,
    ) -> str:
        lines: list[str] = []

        if task.duration_seconds is not None:
            lines.append(f"⏱ Duration: {format_duration(task.duration_seconds)}")
        if task.exit_code is not None:
            lines.append(f"Exit code: {task.exit_code}")
        model = _model_from_export(export) or task.model
        if model:
            lines.append(f"Model: {model}")
        usage = _token_cost(export)
        if usage:
            lines.append(f"Usage:{usage}")

        files = _changed_files(git_before, git_after)
        if files:
            lines.append("")
            lines.append("Changes:")
            for name in files[:_FILES_LIMIT]:
                lines.append(f"• {name}")
            if len(files) > _FILES_LIMIT:
                lines.append(f"• … and {len(files) - _FILES_LIMIT} more")

        commit = _commit_info(git_before, git_after)
        if commit:
            lines.append("")
            lines.append(f"Commit: {commit}")

        all_text = "\n".join((_collect_text(export), log_tail))
        tests = _detect_tests(all_text)
        if tests:
            lines.append("")
            lines.append("Tests:")
            for value in tests:
                lines.append(f"• {value}")

        error_lines = _error_lines(all_text)
        if error_lines:
            lines.append("")
            lines.append("Errors:")
            for value in error_lines:
                lines.append(f"• {value}")

        warnings = len(_WARNING_RE.findall(all_text))
        if warnings:
            lines.append("")
            lines.append(f"Warnings: {warnings}")

        tail = _assistant_tail(export)
        if tail:
            lines.append("")
            lines.append("Summary:")
            lines.append(tail)

        if git_after.is_repo and git_after.is_dirty:
            lines.append("")
            lines.append("⚠ Uncommitted changes present.")

        if not lines:
            lines.append("Task finished. No additional details were captured.")
        return "\n".join(lines)


def _model_from_export(export: dict) -> str | None:
    info = export.get("info") if isinstance(export.get("info"), dict) else {}
    model = info.get("model")
    if isinstance(model, dict):
        provider = model.get("providerID")
        model_id = model.get("id")
        if provider and model_id:
            return f"{provider}/{model_id}"
        return model_id
    return model if isinstance(model, str) else None


def _changed_files(before: GitState, after: GitState) -> list[str]:
    if not after.is_repo:
        return []
    if before.is_repo and before.head != after.head:
        return list(after.short_status) or []
    return list(after.short_status)


def _commit_info(before: GitState, after: GitState) -> str | None:
    if not (before.is_repo and after.is_repo):
        return None
    if before.head and after.head and before.head != after.head:
        return after.head
    return None


def _error_lines(text: str) -> list[str]:
    patterns = (re.compile(r"(?:^|\n)(?:error|ERROR|Traceback|Error)[^\n]{0,160}", re.MULTILINE),)
    seen: list[str] = []
    for pattern in patterns:
        for match in pattern.findall(text):
            line = match.strip()
            if line and line not in seen and len(seen) < 5:
                seen.append(line[:160])
    return seen
