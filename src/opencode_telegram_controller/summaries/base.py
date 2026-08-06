"""Summary generator interface."""

from __future__ import annotations

import abc

from ..models import GitState, Task


class SummaryGenerator(abc.ABC):
    """Builds a concise human-readable summary of a finished task."""

    @abc.abstractmethod
    async def generate(
        self,
        *,
        task: Task,
        export: dict,
        git_before: GitState,
        git_after: GitState,
        log_tail: str,
    ) -> str:
        raise NotImplementedError
