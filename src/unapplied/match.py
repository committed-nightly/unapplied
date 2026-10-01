"""Work out which attributes line matches which path, by asking git.

Gitattributes patterns are wildmatch, which has enough corners -- `**`,
basename-vs-anchored, a trailing slash that matches nothing at all -- that a
reimplementation would be a second thing to be wrong about. So this module
does not implement matching.

Instead it builds a scratch repository containing the same gitattributes
files, in the same directories, with every pattern kept exactly as written
and every attribute replaced by one unique sentinel per source line. Asking
`git check-attr --all` about a path in that repository then reports the
sentinel of every line that matched it. Distinct sentinels mean no line can
override another, so the answer is the full match set rather than the winner.

Git does the matching. We only have to read the answer.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .attrfile import AttrFile
from .gitrepo import _check_attr_all


class ProbeFailed(Exception):
    pass


def resolve_matches(
    files: list[AttrFile],
    paths: list[str],
    ignorecase: bool = False,
) -> None:
    """Fill in `probe` and `matched` on every line of every file, in place."""
    lines = [line for f in files for line in f.lines]
    if not lines:
        return

    for index, line in enumerate(lines):
        line.probe = f"up{index}"
        line.matched = []

    with tempfile.TemporaryDirectory(prefix="unapplied-probe-") as tmp:
        probe_root = Path(tmp)
        _build_probe(probe_root, files, ignorecase)
        attrs = _check_attr_all(probe_root, paths)

    by_probe = {line.probe: line for line in lines}
    for path in paths:
        for name, value in attrs.get(path, {}).items():
            line = by_probe.get(name)
            if line is not None and value != "unspecified":
                line.matched.append(path)


def _build_probe(root: Path, files: list[AttrFile], ignorecase: bool) -> None:
    run = lambda *args: subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    init = run("init", "-q", ".")
    if init.returncode != 0:
        raise ProbeFailed(f"could not create the probe repository: {init.stderr.strip()}")
    # core.ignorecase changes whether a pattern matches, so the probe has to
    # agree with the repository being checked.
    run("config", "core.ignorecase", "true" if ignorecase else "false")

    info_lines: list[str] = []
    for f in files:
        rendered = [f"{_quote(line.pattern)} {line.probe}" for line in f.lines]
        if not rendered:
            continue
        if f.source.startswith("$GIT_DIR/"):
            info_lines.extend(rendered)
            continue
        target = root / f.base / ".gitattributes" if f.base else root / ".gitattributes"
        target.parent.mkdir(parents=True, exist_ok=True)
        # core.attributesFile and the toplevel file share base "", so append.
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(rendered) + "\n")

    if info_lines:
        info = root / ".git" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "attributes").write_text("\n".join(info_lines) + "\n", encoding="utf-8")


def _quote(pattern: str) -> str:
    """Re-quote a pattern so git reads back the characters it started with.

    The pattern arrives already unquoted, so one that contained a space has
    to be given its quotes back or it would be read as a pattern plus a
    stray attribute.
    """
    if not pattern:
        return '""'
    if any(c.isspace() for c in pattern) or pattern.startswith('"'):
        escaped = pattern.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return pattern
