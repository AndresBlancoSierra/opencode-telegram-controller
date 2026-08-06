"""Tests for read-only Git integration."""

from __future__ import annotations

import subprocess

import pytest

from opencode_telegram_controller.gitinfo import (
    changed_files_since,
    commit_created_between,
    git_state,
)
from opencode_telegram_controller.models import GitState


def git(*args, cwd) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo_dir(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    git("init", "-b", "main", cwd=d)
    git("config", "user.email", "test@example.com", cwd=d)
    git("config", "user.name", "Test", cwd=d)
    (d / "a.txt").write_text("hello\n")
    git("add", ".", cwd=d)
    git("commit", "-m", "initial", cwd=d)
    return d


@pytest.fixture
def plain_dir(tmp_path):
    return tmp_path / "not-a-repo"


async def test_git_state_repo(repo_dir):
    state = await git_state(repo_dir)
    assert state.is_repo is True
    assert state.branch == "main"
    assert state.head


async def test_git_state_dirty_worktree(repo_dir):
    (repo_dir / "a.txt").write_text("changed\n")
    state = await git_state(repo_dir)
    assert any("a.txt" in line for line in state.short_status)


async def test_git_state_non_repo(plain_dir):
    plain_dir.mkdir()
    state = await git_state(plain_dir)
    assert state.is_repo is False
    assert state.branch is None


async def test_commit_created_between():
    before = GitState(is_repo=True, head="aaaa")
    after = GitState(is_repo=True, head="bbbb")
    assert await commit_created_between(before, after) == "bbbb"


async def test_no_commit_no_change():
    before = GitState(is_repo=True, head="aaaa")
    assert await commit_created_between(before, before) is None
    assert await commit_created_between(GitState(), GitState()) is None


async def test_changed_files_since(repo_dir):
    (repo_dir / "b.txt").write_text("new\n")
    git("add", ".", cwd=repo_dir)
    git("commit", "-m", "add b", cwd=repo_dir)
    first = git("rev-parse", "HEAD~1", cwd=repo_dir).strip()
    files = await changed_files_since(repo_dir, first)
    assert "b.txt" in files
