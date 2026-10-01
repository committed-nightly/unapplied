"""Thin wrappers over the git commands this tool treats as ground truth.

Every question that git can answer about itself is asked of git. The only
thing computed here is bookkeeping.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class NotARepository(Exception):
    pass


class GitFailed(Exception):
    pass


def _run(args: list[str], cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitFailed(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@dataclass
class EolRecord:
    """One line of `git ls-files --eol`."""

    path: str
    index_eol: str  # lf, crlf, mixed, none, or "" when git did not say
    worktree_eol: str
    attr: str  # the text/eol attribute git resolved, e.g. "text=auto"


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            top = _run(["rev-parse", "--show-toplevel"], path).strip()
        except GitFailed as exc:
            raise NotARepository(f"{path} is not inside a git repository") from exc
        if not top:
            raise NotARepository(f"{path} is not inside a git repository")
        self.root = Path(top)

    def git_dir(self) -> Path:
        out = _run(["rev-parse", "--absolute-git-dir"], self.root).strip()
        return Path(out)

    def has_commits(self) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0

    def tracked_files(self) -> list[str]:
        out = _run(["ls-files", "-z"], self.root)
        return [p for p in out.split("\0") if p]

    def config_get(self, key: str) -> str | None:
        proc = subprocess.run(
            ["git", "config", "--get", key],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    def config_keys(self) -> set[str]:
        """Every config key visible from this repo, lowercased.

        Includes system and global config, because that is where a diff or
        merge driver is usually defined -- and a driver defined only in the
        author's ~/.gitconfig is the whole reason this check is interesting.
        """
        proc = subprocess.run(
            ["git", "config", "--list", "--name-only", "-z"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return set()
        return {k.lower() for k in proc.stdout.split("\0") if k}

    def check_attr_all(self, paths: list[str]) -> dict[str, dict[str, str]]:
        """`git check-attr --all --stdin` for a batch of paths.

        Returns {path: {attribute: value}}. Values are git's own words:
        "set", "unset", "unspecified", or a literal string.
        """
        return _check_attr_all(self.root, paths)

    def ls_files_eol(self) -> list[EolRecord]:
        out = _run(["ls-files", "--eol", "-z"], self.root)
        records = []
        for entry in out.split("\0"):
            if not entry:
                continue
            fields, _, path = entry.partition("\t")
            parts = fields.split()
            index_eol = worktree_eol = attr = ""
            for part in parts:
                if part.startswith("i/"):
                    index_eol = part[2:]
                elif part.startswith("w/"):
                    worktree_eol = part[2:]
                elif part.startswith("attr/"):
                    attr = part[5:]
            records.append(EolRecord(path.strip(), index_eol, worktree_eol, attr))
        return records

    def blob_head(self, path: str, size_limit: int = 512) -> bytes | None:
        """The first `size_limit` bytes of this path's blob in the index."""
        proc = subprocess.run(
            ["git", "cat-file", "--filters", "--path", path, ":" + path],
            cwd=self.root,
            capture_output=True,
        )
        if proc.returncode != 0:
            proc = subprocess.run(
                ["git", "show", ":" + path],
                cwd=self.root,
                capture_output=True,
            )
            if proc.returncode != 0:
                return None
        return proc.stdout[:size_limit]

    def blob_raw(self, path: str, size_limit: int = 512) -> bytes | None:
        """The bytes stored in the index, with no filters applied."""
        oid = self.index_oid(path)
        if oid is None:
            return None
        proc = subprocess.run(
            ["git", "cat-file", "blob", oid],
            cwd=self.root,
            capture_output=True,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout[:size_limit]

    def index_oid(self, path: str) -> str | None:
        proc = subprocess.run(
            ["git", "ls-files", "-s", "--", path],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return proc.stdout.split()[1]


def _check_attr_all(cwd: Path, paths: list[str]) -> dict[str, dict[str, str]]:
    """Run check-attr over paths in batches.

    `--stdin` with NUL separators, because a path can contain a newline and
    this tool is pointed at other people's repositories.
    """
    result: dict[str, dict[str, str]] = {p: {} for p in paths}
    if not paths:
        return result

    batch_size = 2000
    for start in range(0, len(paths), batch_size):
        batch = paths[start : start + batch_size]
        proc = subprocess.run(
            ["git", "check-attr", "--all", "-z", "--stdin"],
            cwd=cwd,
            input="\0".join(batch),
            capture_output=True,
            text=True,
            errors="replace",
        )
        # check-attr writes complaints about malformed lines to stderr and
        # still answers for everything it could parse, so stderr is not fatal.
        fields = proc.stdout.split("\0")
        # -z output is a flat stream of path, attribute, value triples.
        for i in range(0, len(fields) - 2, 3):
            path, attr, value = fields[i], fields[i + 1], fields[i + 2]
            if path in result:
                result[path][attr] = value
    return result
