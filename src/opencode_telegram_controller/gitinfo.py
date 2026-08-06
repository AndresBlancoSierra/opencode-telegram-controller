"""Read-only Git integration.

The controller never commits or pushes on its own. This module only reports
repository state so tasks and summaries can include branch, HEAD and changed
files information.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .models import GitState

_GIT_TIMEOUT = 10.0


async def _git(path: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(path),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    return stdout.decode(errors="replace").strip()


async def git_state(path: Path) -> GitState:
    """Return a snapshot of the repository at ``path`` (empty state if not a repo)."""
    if not await _git(path, "rev-parse", "--is-inside-work-tree"):
        return GitState()
    branch = await _git(path, "branch", "--show-current")
    head = await _git(path, "rev-parse", "--short", "HEAD")
    status_text = await _git(path, "status", "--porcelain")
    short_status = [line for line in status_text.splitlines() if line.strip()][:50]
    return GitState(
        is_repo=True,
        branch=branch or None,
        head=head or None,
        short_status=short_status,
    )


async def commit_created_between(before: GitState, after: GitState) -> str | None:
    """Return the short SHA of the new HEAD commit, if one was created."""
    if not before.is_repo or not after.is_repo:
        return None
    if before.head and after.head and before.head != after.head:
        return after.head
    return None


async def changed_files_since(path: Path, since_sha: str) -> list[str]:
    """Files changed between ``since_sha`` and the current working tree."""
    names = await _git(path, "diff", "--name-only", f"{since_sha}..HEAD", "--")
    result = [n for n in names.splitlines() if n.strip()]
    dirty = await _git(path, "status", "--porcelain")
    for line in dirty.splitlines():
        if line.strip() and line[:2].strip():
            result.append(line[3:])
    seen: list[str] = []
    for name in result:
        if name not in seen:
            seen.append(name)
    return seen[:100]
