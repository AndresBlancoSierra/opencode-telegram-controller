"""Application entry point.

Wires configuration, persistence, the OpenCode adapter, task machinery and the
Telegram bot together, then starts long polling.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Dispatcher
from loguru import logger

from . import __version__
from .auth import AuthorizationService
from .bot import AppContext, build_bot, build_router
from .config import Settings, load_settings
from .database import Database
from .logging_setup import setup_logging
from .notifications import NotificationManager
from .opencode import CLIOpenCodeAdapter
from .projects import ProjectConfigError, ProjectRegistry
from .queue_worker import QueueWorker
from .repository import TaskRepository
from .summaries import DeterministicSummaryGenerator, OllamaSummaryGenerator
from .summaries.base import SummaryGenerator
from .task_executor import TaskExecutor
from .task_manager import TaskManager

_KNOWN_OPENCODE_PATHS = (
    Path.home() / ".local" / "share" / "mise" / "installs" / "node" / "latest" / "bin" / "opencode",
    Path("/usr/local/bin/opencode"),
    Path("/usr/bin/opencode"),
)


def resolve_opencode_bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for candidate in _KNOWN_OPENCODE_PATHS:
        if candidate.exists():
            return str(candidate)
    return name


def build_summary_generator(settings: Settings) -> SummaryGenerator:
    deterministic = DeterministicSummaryGenerator()
    if settings.summary_engine == "ollama":
        return OllamaSummaryGenerator(
            url=settings.ollama_url,
            model=settings.ollama_model,
            fallback=deterministic,
        )
    return deterministic


async def amain() -> None:
    settings = load_settings()
    setup_logging(settings)
    logger.info("OpenCode Telegram Controller v{} starting", __version__)

    db = Database(settings.database_path)
    await db.connect()
    repo = TaskRepository(db)

    try:
        registry = ProjectRegistry.from_file(settings.projects_file)
    except ProjectConfigError as exc:
        logger.error("Invalid projects configuration: {}", exc)
        raise SystemExit(1) from exc
    logger.info("Loaded {} projects from {}", len(registry.projects), settings.projects_file)

    bot = build_bot(settings)
    notifier = NotificationManager(bot, chat_ids=settings.allowed_user_ids)
    auth = AuthorizationService(settings.allowed_user_ids, on_security_event=notifier.send)

    opencode_bin = resolve_opencode_bin(settings.opencode_bin)
    logger.info("Using OpenCode binary: {}", opencode_bin)
    adapter = CLIOpenCodeAdapter(
        binary=opencode_bin,
        model=settings.opencode_model,
        agent=settings.opencode_agent,
        extra_args=settings.opencode_extra_args,
    )

    summary_generator = build_summary_generator(settings)
    executor = TaskExecutor(
        adapter=adapter,
        repo=repo,
        registry=registry,
        notifier=notifier,
        summary_generator=summary_generator,
        settings=settings,
    )
    worker = QueueWorker(
        repo=repo,
        registry=registry,
        executor=executor,
        notifier=notifier,
        settings=settings,
    )
    manager = TaskManager(
        repo=repo,
        registry=registry,
        notifier=notifier,
        executor=executor,
        settings=settings,
        adapter=adapter,
    )
    ctx = AppContext(
        settings=settings,
        repo=repo,
        registry=registry,
        auth=auth,
        manager=manager,
        executor=executor,
        worker=worker,
        notifier=notifier,
        started_at=datetime.now(UTC),
    )

    interrupted = await repo.recover_interrupted()
    for task_id in interrupted:
        task = await repo.get_task(task_id)
        if task is not None:
            await notifier.notify_task_failed(task)

    router = build_router(ctx)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    worker.start()
    logger.info("Telegram polling started (long polling, no exposed ports)")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)
    finally:
        logger.info("Shutting down")
        await worker.stop()
        await db.close()
        await bot.session.close()


def cli() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        logger.info("Interrupted")
