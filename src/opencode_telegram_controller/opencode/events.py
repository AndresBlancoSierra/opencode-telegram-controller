"""Parsing of the ``opencode run --format json`` NDJSON event stream.

Each stdout line is a JSON object with at least ``type`` and ``sessionID``;
many events also embed a ``part`` object (the same shape used by the OpenCode
SDK). Event types observed with OpenCode 1.15.13 include ``step_start``,
``text``, ``tool_use``, ``step_finish`` and others.
"""

from __future__ import annotations

import json

from .base import OpenCodeEvent


def parse_line(line: str) -> OpenCodeEvent | None:
    """Parse one NDJSON line into an :class:`OpenCodeEvent` (None on garbage)."""
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    part = data.get("part")
    part_type = part.get("type") if isinstance(part, dict) else None
    text = part.get("text") if isinstance(part, dict) else None
    return OpenCodeEvent(
        type=str(data.get("type", "")),
        session_id=data.get("sessionID"),
        part_type=part_type,
        text=text if isinstance(text, str) else None,
        raw=data,
    )


def is_text_event(event: OpenCodeEvent) -> bool:
    """True when an event carries assistant or tool output text worth reporting."""
    return bool(event.text) and event.type == "text"
