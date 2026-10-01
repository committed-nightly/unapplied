"""The claims this tool makes about git, asserted against the git on this box.

Everything unapplied reports rests on one of these behaviours being true. If
a future git changes its mind, these fail first and loudly -- which is the
point. A tool that quietly keeps reporting a finding git no longer earns is
worse than one that will not start.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
from pathlib import Path

import pytest

from unapplied.drivers import BUILTIN_DIFF


def test_trailing_slash_pattern_matches_nothing(repo):
    """The claim behind the hint on a `vendor/` pattern."""
    repo.write("vendor/thing.c", "x\n")
    repo.write(".gitattributes", "vendor/ marked\nvendor/** alsomarked\n")
    repo.commit()

    out = repo.git("check-attr", "--all", "vendor/thing.c")
    assert "alsomarked" in out, "sanity: vendor/** should match"
    assert "marked: set" not in out.replace("alsomarked: set", ""), (
        "git applied a trailing-slash gitattributes pattern; the never-matches "
        "hint telling people to use vendor/** is now wrong"
    )


def test_adding_text_attribute_does_not_renormalize_history(repo):
    """The whole premise of the unnormalized check."""
    repo.write("win.txt", b"a\r\nb\r\n")
    repo.commit()
    repo.write(".gitattributes", "* text=auto\n")
    repo.commit_only("attrs", ".gitattributes")

    assert "text: auto" in repo.git("check-attr", "--all", "win.txt")
    eol = repo.git("ls-files", "--eol", "--", "win.txt")
    assert "i/crlf" in eol, (
        "git normalised an already-committed file when the attribute was added; "
        "the unnormalized check no longer describes anything real"
    )
    assert repo.git("status", "--porcelain") == "", (
        "git now reports the un-normalised file as modified, which would make "
        "this tool redundant for that case"
    )


def test_undefined_diff_driver_is_silent(repo):
    """The claim behind undefined-driver."""
    repo.write("f.c", "a\n")
    repo.write(".gitattributes", "*.c diff=nosuchdriveranywhere\n")
    repo.commit()
    repo.write("f.c", "a\nb\n")

    proc = subprocess.run(
        ["git", "diff"], cwd=repo.path, capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert proc.stderr == "", (
        f"git now warns about an undefined diff driver ({proc.stderr.strip()!r}), "
        "so its warning is better than this tool's finding"
    )


def test_undefined_filter_driver_is_silent(repo):
    repo.write("f.bin", "content\n")
    repo.write(".gitattributes", "*.bin filter=nosuchfilteranywhere\n")
    proc = subprocess.run(
        ["git", "add", "-A"], cwd=repo.path, capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert "nosuchfilteranywhere" not in proc.stderr


def test_macro_outside_toplevel_is_already_loud(repo):
    """Why there is no check for this.

    git rejects it by name and line number, which is strictly better than
    anything unapplied would print, so the check was dropped.
    """
    repo.write("sub/f.c", "x\n")
    repo.write("sub/.gitattributes", "[attr]mymacro text\n*.c mymacro\n")
    repo.commit()

    proc = subprocess.run(
        ["git", "check-attr", "--all", "sub/f.c"],
        cwd=repo.path,
        capture_output=True,
        text=True,
    )
    assert "not allowed" in proc.stderr and "sub/.gitattributes:1" in proc.stderr, (
        "git stopped complaining about a macro outside the toplevel attributes "
        "file, so unapplied should probably grow that check"
    )


def test_info_attributes_outranks_the_deepest_directory(repo):
    """The precedence order in attrfile.discover()."""
    repo.write("sub/f.c", "x\n")
    repo.write(".gitattributes", "*.c k=toplevel\n")
    repo.write("sub/.gitattributes", "*.c k=sub\n")
    repo.commit()
    info = repo.path / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text("*.c k=info\n", encoding="utf-8")

    assert "k: info" in repo.git("check-attr", "--all", "sub/f.c")


def test_one_invalid_attribute_kills_the_whole_line(repo):
    """Why `unparsable` reports the line rather than the single bad token."""
    repo.write("f.b", "x\n")
    repo.write(".gitattributes", "*.b goodattr =noname\n")
    repo.commit()

    proc = subprocess.run(
        ["git", "check-attr", "--all", "f.b"],
        cwd=repo.path,
        capture_output=True,
        text=True,
    )
    assert "goodattr" not in proc.stdout, (
        "git now applies the valid attributes on a line with an invalid one, so "
        "unparsable overstates the damage"
    )


def _builtin_drivers_from_man() -> set[str] | None:
    """Read git's own list of built-in diff drivers, if the man page is here."""
    for candidate in (
        Path("/usr/share/man/man5/gitattributes.5.gz"),
        Path("/usr/local/share/man/man5/gitattributes.5.gz"),
    ):
        if candidate.is_file():
            text = gzip.decompress(candidate.read_bytes()).decode("utf-8", "replace")
            break
    else:
        if not shutil.which("man"):
            return None
        proc = subprocess.run(
            ["man", "gitattributes"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "MANWIDTH": "200"},
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        text = proc.stdout

    marker = "The following built in patterns are available"
    if marker not in text:
        return None
    tail = text.split(marker, 1)[1]
    # Stop before the next section so unrelated bold words are not collected.
    tail = tail.split("Customizing word diff", 1)[0]
    names = set()
    for line in tail.splitlines():
        line = line.strip()
        if line.startswith("\\fB") and line.endswith("\\fR"):
            names.add(line[3:-3])
        else:
            cleaned = "".join(c for c in line if c.isprintable() and c != "\b")
            if cleaned and cleaned.replace("-", "").replace("+", "").isalnum() and cleaned.islower():
                names.add(cleaned)
    return names or None


def test_builtin_diff_driver_list_matches_this_git():
    """The one hardcoded list in the tool, checked against the installed git.

    A git that grows a driver would make unapplied call a live driver dead.
    """
    from_man = _builtin_drivers_from_man()
    if from_man is None:
        pytest.skip("gitattributes(5) is not readable on this box")
    missing = from_man - BUILTIN_DIFF
    assert not missing, (
        f"gitattributes(5) on this box lists built-in diff drivers that "
        f"drivers.BUILTIN_DIFF does not: {sorted(missing)}"
    )
