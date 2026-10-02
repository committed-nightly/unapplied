"""Parse gitattributes files into lines, patterns and assignments.

Only the shape of a line is worked out here -- which part is the pattern and
which parts are attribute assignments. Whether a pattern matches anything is
git's job, not this module's; see `match.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Git's own rule: an attribute name is made of [-A-Za-z0-9_.] and may not
# begin with a dash or a digit. attr.c, attr_name_valid().
_NAME = re.compile(r"\A[A-Za-z_.][-A-Za-z0-9_.]*\Z")

#: Attributes git looks up per file, because they are about file content.
#: Setting one of these on a pattern that only ever matches a directory does
#: nothing: git asks for a directory's attributes only while walking a tree,
#: and never asks about its content.
PER_FILE_ATTRIBUTES = frozenset(
    {
        "text",
        "eol",
        "working-tree-encoding",
        "ident",
        "filter",
        "diff",
        "merge",
        "conflict-marker-size",
        "whitespace",
        "export-subst",
        "delta",
        "encoding",
    }
)

#: The one git attribute a directory pattern is the normal way to write.
#: `git archive` looks up export-ignore on tree entries and drops the whole
#: subtree, so `/Tests export-ignore` and even `Tests/ export-ignore` work
#: despite matching no file -- verified against git 2.55.
DIRECTORY_ATTRIBUTES = frozenset({"export-ignore"})


@dataclass(frozen=True)
class Assignment:
    """One `name`, `-name`, `!name` or `name=value` on a line."""

    name: str
    value: str  # "set", "unset", "unspecified", or the literal value
    #: the macro this came out of, when it was not written on the line
    via: str | None = None

    @property
    def is_driver_ref(self) -> bool:
        return self.name in ("diff", "merge", "filter") and self.value not in (
            "set",
            "unset",
            "unspecified",
        )

    def describe(self) -> str:
        if self.via:
            return f"{self.name} (via the {self.via} macro)"
        return self.name


#: Macros git defines itself. `binary` both sets `binary` and expands, which
#: is why a repository full of `*.png binary` resolves to text being unset
#: rather than to whatever `* text=auto` said.
BUILTIN_MACROS: dict[str, tuple[Assignment, ...]] = {
    "binary": (
        Assignment("diff", "unset"),
        Assignment("merge", "unset"),
        Assignment("text", "unset"),
    ),
}


def expand(assignments: list[Assignment], macros: dict[str, tuple[Assignment, ...]]) -> list[Assignment]:
    """Resolve macros to the attributes git will actually apply.

    Verified against git 2.55:

    * a macro sets its own name *and* applies its attributes
      (`*.png binary` gives `binary: set` plus `-diff -merge -text`)
    * a negated macro sets the name and does *not* expand
      (`-binary` leaves `text=auto` from an earlier line alone)
    * a macro may be used above the line that defines it
    * a macro may expand to another macro
    """
    out: list[Assignment] = []

    def walk(assignment: Assignment, seen: frozenset[str], via: str | None) -> None:
        out.append(
            Assignment(assignment.name, assignment.value, via)
            if via
            else assignment
        )
        if assignment.value != "set" or assignment.name not in macros:
            return
        if assignment.name in seen:
            return  # a macro defined in terms of itself; git ignores the loop
        for inner in macros[assignment.name]:
            walk(inner, seen | {assignment.name}, via or assignment.name)

    for assignment in assignments:
        walk(assignment, frozenset(), None)
    return out


@dataclass
class AttrLine:
    """A single line of a gitattributes file that carries a path pattern."""

    source: str  # display path of the file, e.g. "sub/.gitattributes"
    lineno: int
    raw: str
    pattern: str
    assignments: list[Assignment]
    #: directory the pattern is relative to, "" for the repository root
    base: str
    #: precedence rank, higher wins; set by the caller that orders the files
    rank: int = 0
    probe: str = ""  # sentinel attribute name used to ask git what this matches
    matched: list[str] = field(default_factory=list)
    #: tracked directories the pattern matches, which is a different question:
    #: only export-ignore survives on a pattern that matches no file
    matched_dirs: list[str] = field(default_factory=list)

    @property
    def location(self) -> str:
        return f"{self.source}:{self.lineno}"


@dataclass
class AttrFile:
    source: str
    base: str
    rank: int
    lines: list[AttrLine]
    #: names defined by [attr] lines in this file, mapped to their attributes
    macros: dict[str, tuple[Assignment, ...]]
    unparsed: list[tuple[int, str, str]]  # lineno, raw, why


def _split_tokens(line: str) -> list[str] | None:
    """Split a line into pattern plus attributes, honouring a quoted pattern.

    Git parses the pattern with its C-quoting unquoter when it starts with a
    double quote, so `"with space.txt"` is one pattern. Returns None if the
    quoting does not close.
    """
    line = line.strip()
    if not line.startswith('"'):
        return line.split()

    out = []
    i = 1
    buf = []
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            buf.append(line[i + 1])
            i += 2
            continue
        if ch == '"':
            out.append("".join(buf))
            rest = line[i + 1 :].split()
            return out + rest
        buf.append(ch)
        i += 1
    return None


def parse(text: str, source: str, base: str, rank: int) -> AttrFile:
    lines: list[AttrLine] = []
    macros: dict[str, tuple[Assignment, ...]] = {}
    unparsed: list[tuple[int, str, str]] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        tokens = _split_tokens(stripped)
        if tokens is None:
            unparsed.append((lineno, stripped, "unterminated quoted pattern"))
            continue
        if not tokens:
            continue

        pattern, *rest = tokens

        if pattern.startswith("[attr]"):
            name = pattern[len("[attr]") :]
            if name and base == "":
                # Only the toplevel file and info/attributes may define
                # macros. Elsewhere git rejects the line by name and line
                # number, which is louder than anything this tool would say.
                defined = [a for a in (_parse_assignment(t) for t in rest) if a]
                macros[name] = tuple(defined)
            continue

        assignments: list[Assignment] = []
        bad = None
        for token in rest:
            assignment = _parse_assignment(token)
            if assignment is None:
                bad = token
                break
            assignments.append(assignment)

        if bad is not None:
            # Git rejects the whole line when any attribute name on it is
            # invalid, so every assignment on the line is dead, not just the
            # bad one. Verified against git 2.55: a line with `=noname` on it
            # applies none of its other attributes.
            unparsed.append((lineno, stripped, f"{bad!r} is not a valid attribute"))
            continue

        if not assignments:
            unparsed.append((lineno, stripped, "pattern with no attributes"))
            continue

        lines.append(
            AttrLine(
                source=source,
                lineno=lineno,
                raw=stripped,
                pattern=pattern,
                assignments=assignments,
                base=base,
                rank=rank,
            )
        )

    return AttrFile(source=source, base=base, rank=rank, lines=lines, macros=macros, unparsed=unparsed)


def _parse_assignment(token: str) -> Assignment | None:
    if token.startswith("-"):
        name, value = token[1:], "unset"
    elif token.startswith("!"):
        name, value = token[1:], "unspecified"
    elif "=" in token:
        name, _, value = token.partition("=")
    else:
        name, value = token, "set"

    if not _NAME.match(name):
        return None
    return Assignment(name=name, value=value)


def discover(root: Path, git_dir: Path, tracked: set[str], attributes_file: str | None) -> list[AttrFile]:
    """Every gitattributes source git will read, lowest precedence first.

    Order is git's: core.attributesFile, then the toplevel .gitattributes,
    then each deeper directory, then $GIT_DIR/info/attributes last -- which
    verifiably outranks even the deepest directory (git 2.55).
    """
    sources: list[tuple[str, str, Path]] = []

    if attributes_file:
        expanded = Path(attributes_file).expanduser()
        if expanded.is_file():
            sources.append((str(expanded), "", expanded))

    in_tree = sorted(
        (p for p in tracked if p == ".gitattributes" or p.endswith("/.gitattributes")),
        key=lambda p: (p.count("/"), p),
    )
    for rel in in_tree:
        base = rel[: -len(".gitattributes")].rstrip("/")
        sources.append((rel, base, root / rel))

    info = git_dir / "info" / "attributes"
    if info.is_file():
        sources.append(("$GIT_DIR/info/attributes", "", info))

    files = []
    for rank, (display, base, path) in enumerate(sources):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append(parse(text, display, base, rank))
    return files
