"""Optional LLM-based summary generator using a local Ollama instance.

Disabled by default. When ``OTC_SUMMARY_ENGINE=ollama`` and Ollama is reachable
at ``OTC_OLLAMA_URL``, the session transcript is summarized locally. Any failure
falls back to the deterministic summary so the system keeps working without an
LLM.
"""

from __future__ import annotations

import httpx
from loguru import logger

from ..models import GitState, Task
from .base import SummaryGenerator

_SYSTEM_PROMPT = (
    "You summarize coding tasks executed by an AI coding agent. "
    "Be concise, factual and structured. Report what was done, key changes, "
    "test results if visible, errors, and any git commit created. "
    "Use plain text, no markdown headings."
)


class OllamaSummaryGenerator(SummaryGenerator):
    def __init__(self, *, url: str, model: str, fallback: SummaryGenerator, timeout: float = 60.0):
        self._url = url.rstrip("/")
        self._model = model
        self._fallback = fallback
        self._timeout = timeout

    async def generate(
        self,
        *,
        task: Task,
        export: dict,
        git_before: GitState,
        git_after: GitState,
        log_tail: str,
    ) -> str:
        prompt = self._build_prompt(task, export, git_before, git_after, log_tail)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._url}/api/generate",
                    json={
                        "model": self._model,
                        "prompt": prompt,
                        "system": _SYSTEM_PROMPT,
                        "stream": False,
                    },
                )
                response.raise_for_status()
                data = response.json()
                text = str(data.get("response", "")).strip()
                if text:
                    return text
                logger.warning("Ollama returned an empty summary; using deterministic fallback")
        except Exception:
            logger.exception("Ollama summary failed; using deterministic fallback")
        return await self._fallback.generate(
            task=task,
            export=export,
            git_before=git_before,
            git_after=git_after,
            log_tail=log_tail,
        )

    @staticmethod
    def _build_prompt(
        task: Task, export: dict, git_before: GitState, git_after: GitState, log_tail: str
    ) -> str:
        return (
            f"Task prompt: {task.prompt}\n\n"
            f"Project: {task.project_id}\n"
            f"Exit code: {task.exit_code}\n"
            f"Git: branch={git_after.branch} head={git_after.head} "
            f"(was {git_before.head})\n"
            f"Git status:\n{chr(10).join(git_after.short_status[:30]) or '(clean)'}\n\n"
            f"Session export (JSON):\n{export}\n\n"
            f"Log tail:\n{log_tail[-4000:]}\n\n"
            "Write the summary now."
        )
