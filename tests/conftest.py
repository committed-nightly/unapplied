from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


class Builder:
    """A throwaway git repository to point the tool at."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q", "-b", "main", ".")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        # The box's global config must not decide what these tests prove.
        # core.autocrlf would normalise on add without any attribute, and a
        # globally installed git-lfs would make filter=lfs look configured.
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.ignorecase", "false")

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
        return proc.stdout

    def write(self, rel: str, content: str | bytes) -> Path:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        return target

    def commit(self, message: str = "c", *paths: str) -> None:
        self.git("add", "--", *(paths or ("-A",))) if paths else self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def commit_only(self, message: str, *paths: str) -> None:
        """Stage exactly these paths. The realistic way attributes get added."""
        self.git("add", "--", *paths)
        self.git("commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Builder:
    return Builder(tmp_path)


@pytest.fixture
def run():
    """Run the CLI in a repository and return (exit code, stdout, stderr)."""

    def _run(path: Path, *args: str) -> tuple[int, str, str]:
        import io
        import contextlib

        from unapplied.cli import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(path), *args])
        return code, out.getvalue(), err.getvalue()

    return _run
