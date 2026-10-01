"""The checks.

Every finding here has to clear one bar: git is silent about it. If git
already warns -- a macro defined outside the toplevel file, say, which it
rejects with `[attr]x not allowed: sub/.gitattributes:1` -- then the warning
is better than anything this tool could add, and it is not checked here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import attrfile, drivers
from .attrfile import AttrFile, AttrLine
from .gitrepo import Repo

LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


@dataclass
class Finding:
    check: str
    location: str
    message: str
    detail: list[str] = field(default_factory=list)

    def sort_key(self) -> tuple:
        source, _, lineno = self.location.rpartition(":")
        try:
            n = int(lineno)
        except ValueError:
            source, n = self.location, 0
        return (source, n, self.check)


@dataclass
class Model:
    """What this tool believes about the repository, before reporting."""

    files: list[AttrFile]
    paths: list[str]
    #: {path: {attribute: winning AttrLine}}
    winners: dict[str, dict[str, AttrLine]]
    #: every macro in scope, built-in and user-defined
    macros: dict[str, tuple] = field(default_factory=dict)
    #: {line id: assignments after macro expansion}
    effective: dict[int, list] = field(default_factory=dict)

    def assignments_of(self, line: AttrLine) -> list:
        return self.effective.get(id(line), line.assignments)


def collect_macros(files: list[AttrFile]) -> dict[str, tuple]:
    """Macros git has in scope, later definitions winning over earlier ones."""
    macros = dict(attrfile.BUILTIN_MACROS)
    for f in files:
        macros.update(f.macros)
    return macros


def build_model(files: list[AttrFile], paths: list[str]) -> Model:
    """Resolve, per path and attribute, which line git will let win.

    Across files, higher rank wins -- discover() already ordered them from
    lowest precedence to highest. Within one file, the later line wins.
    """
    macros = collect_macros(files)
    lines = [line for f in files for line in f.lines]
    effective = {id(line): attrfile.expand(line.assignments, macros) for line in lines}
    ordered = sorted(lines, key=lambda line: (line.rank, line.lineno))

    winners: dict[str, dict[str, AttrLine]] = {p: {} for p in paths}
    for line in ordered:
        for path in line.matched:
            slot = winners.setdefault(path, {})
            for assignment in effective[id(line)]:
                slot[assignment.name] = line
    return Model(
        files=files, paths=paths, winners=winners, macros=macros, effective=effective
    )


def disagreements(model: Model, repo: Repo) -> list[str]:
    """Cross-check the model against `git check-attr --all` on the real repo.

    This is the tool checking itself. Precedence between attributes files,
    later-line-wins and the match sets are all modelled here, and if any of
    that is wrong then every finding below is suspect. So rather than report
    findings built on a broken model, say so and stop.
    """
    truth = repo.check_attr_all(model.paths)
    problems = []
    for path in model.paths:
        expected = {}
        for name, line in model.winners.get(path, {}).items():
            value = next(
                a.value for a in reversed(model.assignments_of(line)) if a.name == name
            )
            if value != "unspecified":
                expected[name] = value
        actual = {k: v for k, v in truth.get(path, {}).items() if v != "unspecified"}
        if expected != actual:
            only_ours = {k: v for k, v in expected.items() if actual.get(k) != v}
            only_git = {k: v for k, v in actual.items() if expected.get(k) != v}
            problems.append(f"{path}: this tool says {only_ours or '{}'}, git says {only_git or '{}'}")
    return problems


def check_unparsed(files: list[AttrFile]) -> list[Finding]:
    out = []
    for f in files:
        for lineno, raw, why in f.unparsed:
            out.append(
                Finding(
                    check="unparsable",
                    location=f"{f.source}:{lineno}",
                    message=f"git applies nothing from this line: {why}",
                    detail=[raw],
                )
            )
    return out


def check_never_matches(files: list[AttrFile], tracked_count: int) -> list[Finding]:
    """Patterns that match no tracked path at all, file or directory."""
    out = []
    for f in files:
        for line in f.lines:
            if line.matched or line.matched_dirs:
                continue
            hint = _never_matches_hint(line)
            out.append(
                Finding(
                    check="never-matches",
                    location=line.location,
                    message=f"pattern {line.pattern!r} matches nothing tracked "
                    f"({tracked_count} files and their directories)"
                    + (f" -- {hint}" if hint else ""),
                    detail=[line.raw],
                )
            )
    return out


def check_directory_only(model: Model, files: list[AttrFile]) -> list[Finding]:
    """A pattern matching only directories, carrying per-file attributes.

    This is the finding that `never-matches` used to get wrong. A pattern like
    `/Tests` or `vendor/` matches no file, but git does ask about directories
    while walking a tree, so `export-ignore` on one works and `git archive`
    honours it. Anything about file *content* does not: git never asks a
    directory what its line endings are.
    """
    out = []
    for f in files:
        for line in f.lines:
            if line.matched or not line.matched_dirs:
                continue
            dead = [
                a
                for a in model.assignments_of(line)
                if a.name in attrfile.PER_FILE_ATTRIBUTES
            ]
            if not dead:
                continue  # export-ignore only: this pattern is doing its job
            names = ", ".join(sorted({a.describe() for a in dead}))
            suggestion = line.pattern.rstrip("/") + "/**"
            # Both spellings of each directory were probed, so count the
            # directories rather than the matches.
            dirs = sorted({d.rstrip("/") for d in line.matched_dirs})
            noun = "directory" if len(dirs) == 1 else "directories"
            out.append(
                Finding(
                    check="directory-only",
                    location=line.location,
                    message=f"{names} set on a pattern that matches {len(dirs)} "
                    f"{noun} and no file; git reads these off a file, never off "
                    f"a directory, so they do nothing here -- {suggestion} would",
                    detail=[line.raw] + [f"matched: {d}" for d in dirs[:5]],
                )
            )
    return out


def _never_matches_hint(line: AttrLine) -> str | None:
    pattern = line.pattern
    if line.base and pattern.startswith(line.base + "/"):
        return (
            f"patterns are relative to {line.source}, so this looks for "
            f"{line.base}/{pattern}"
        )
    return None


def check_overridden(model: Model) -> list[Finding]:
    """A line whose assignment never wins for any file it matches."""
    out = []
    lines = [line for f in model.files for line in f.lines]
    for line in lines:
        if not line.matched:
            continue  # never-matches already covers it
        for assignment in model.assignments_of(line):
            beaten_by: dict[str, int] = {}
            for path in line.matched:
                winner = model.winners.get(path, {}).get(assignment.name)
                if winner is not None and winner is not line:
                    beaten_by[winner.location] = beaten_by.get(winner.location, 0) + 1
            if len(beaten_by) == 0:
                continue
            if sum(beaten_by.values()) < len(line.matched):
                continue  # still wins somewhere, so the line is doing a job
            where = ", ".join(sorted(beaten_by))
            out.append(
                Finding(
                    check="overridden",
                    location=line.location,
                    message=f"{assignment.describe()} is overridden for all "
                    f"{len(line.matched)} matching files by {where}",
                    detail=[line.raw],
                )
            )
    return out


def check_drivers(files: list[AttrFile], config_keys: set[str]) -> list[Finding]:
    out = []
    for f in files:
        for line in f.lines:
            for assignment in line.assignments:
                if not assignment.is_driver_ref:
                    continue
                kind, name = assignment.name, assignment.value
                if (
                    drivers.is_builtin(kind, name)
                    or drivers.is_conventional(kind, name)
                    or drivers.is_configured(kind, name, config_keys)
                ):
                    continue
                out.append(
                    Finding(
                        check="undefined-driver",
                        location=line.location,
                        message=f"{kind}={name} names a driver that does not exist: "
                        + drivers.explain_missing(kind, name),
                        detail=[line.raw],
                    )
                )
    return out


def check_unnormalized(repo: Repo, model: Model) -> list[Finding]:
    """Attributes say the file is text; the committed blob still has CRLF.

    This is the one that costs an afternoon. Adding `* text=auto` changes
    nothing about what is already committed -- the normalisation happens on
    the next `git add` of each file, and `git add --renormalize` is the step
    everyone's instructions include and nobody runs. Afterwards check-attr
    says text=auto, `git status` is clean, and the blobs in history are still
    CRLF. Nothing in git mentions it.
    """
    out = []
    offenders: dict[str, list[str]] = {}
    for record in repo.ls_files_eol():
        # git's own resolution decides, not this tool's model. The attr field
        # of `ls-files --eol` is what git concluded after macros, precedence
        # and its own binary sniffing, and the i/ field is what is stored.
        #
        # Three things verified against git 2.55, each of which was a false
        # positive before it was checked:
        #   * `-text` means no conversion ever, so a CRLF blob is correct and
        #     an `eol` set alongside it is inert
        #   * a file git considers binary reports `i/-text`, even under
        #     text=auto, so it never reaches the comparison below
        #   * `eol=crlf` still wants LF in the index -- it converts on
        #     checkout, so a CRLF blob under it is unnormalised too
        if record.index_eol not in ("crlf", "mixed"):
            continue
        if not record.attr or "-text" in record.attr.split():
            continue

        attrs = model.winners.get(record.path, {})
        line = attrs.get("text") or attrs.get("eol")
        if line is None:
            continue
        offenders.setdefault(line.location, []).append(f"{record.path} ({record.index_eol})")

    for location, paths in sorted(offenders.items()):
        shown = sorted(paths)
        out.append(
            Finding(
                check="unnormalized",
                location=location,
                message=f"{len(paths)} file(s) this rule covers are still stored with CRLF "
                "in the index -- the rule was added after they were committed and "
                "`git add --renormalize .` was never run",
                detail=shown,
            )
        )
    return out


def check_lfs_pointers(repo: Repo, model: Model) -> list[Finding]:
    """filter=lfs is set, but the stored blob is the real file, not a pointer.

    Same shape as unnormalized and worse consequences: the fat blob is in
    history, the repository is the size it was trying not to be, and every
    check says LFS is configured.
    """
    out = []
    offenders: dict[str, list[str]] = {}
    for path, attrs in model.winners.items():
        line = attrs.get("filter")
        if line is None or _resolved_value(model, line, "filter") != "lfs":
            continue
        blob = repo.blob_raw(path, size_limit=len(LFS_POINTER) + 8)
        if blob is None or blob.startswith(LFS_POINTER):
            continue
        offenders.setdefault(line.location, []).append(path)

    for location, paths in sorted(offenders.items()):
        shown = sorted(paths)
        out.append(
            Finding(
                check="lfs-not-pointer",
                location=location,
                message=f"{len(paths)} file(s) this rule covers are stored as their own "
                "contents, not as LFS pointers -- the rule was added after they were "
                "committed, so LFS never took them",
                detail=shown,
            )
        )
    return out


def _resolved_value(model: Model, line: AttrLine, name: str) -> str | None:
    for assignment in reversed(model.assignments_of(line)):
        if assignment.name == name:
            return assignment.value
    return None


ALL_CHECKS = (
    "unparsable",
    "never-matches",
    "directory-only",
    "overridden",
    "undefined-driver",
    "unnormalized",
    "lfs-not-pointer",
)


def run_all(repo: Repo, files: list[AttrFile], model: Model, enabled: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    if "unparsable" in enabled:
        findings += check_unparsed(files)
    if "never-matches" in enabled:
        findings += check_never_matches(files, len(model.paths))
    if "directory-only" in enabled:
        findings += check_directory_only(model, files)
    if "overridden" in enabled:
        findings += check_overridden(model)
    if "undefined-driver" in enabled:
        findings += check_drivers(files, repo.config_keys())
    if "unnormalized" in enabled:
        findings += check_unnormalized(repo, model)
    if "lfs-not-pointer" in enabled:
        findings += check_lfs_pointers(repo, model)
    findings.sort(key=Finding.sort_key)
    return findings
