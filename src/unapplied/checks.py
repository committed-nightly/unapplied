"""The checks.

Every finding here has to clear one bar: git is silent about it. If git
already warns -- a macro defined outside the toplevel file, say, which it
rejects with `[attr]x not allowed: sub/.gitattributes:1` -- then the warning
is better than anything this tool could add, and it is not checked here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import drivers
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


def build_model(files: list[AttrFile], paths: list[str]) -> Model:
    """Resolve, per path and attribute, which line git will let win.

    Across files, higher rank wins -- discover() already ordered them from
    lowest precedence to highest. Within one file, the later line wins.
    """
    lines = [line for f in files for line in f.lines]
    ordered = sorted(lines, key=lambda line: (line.rank, line.lineno))

    winners: dict[str, dict[str, AttrLine]] = {p: {} for p in paths}
    for line in ordered:
        for path in line.matched:
            slot = winners.setdefault(path, {})
            for assignment in line.assignments:
                slot[assignment.name] = line
    return Model(files=files, paths=paths, winners=winners)


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
            value = next(a.value for a in reversed(line.assignments) if a.name == name)
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
    out = []
    for f in files:
        for line in f.lines:
            if line.matched:
                continue
            hint = _never_matches_hint(line)
            out.append(
                Finding(
                    check="never-matches",
                    location=line.location,
                    message=f"pattern {line.pattern!r} matches none of the {tracked_count} tracked files"
                    + (f" -- {hint}" if hint else ""),
                    detail=[line.raw],
                )
            )
    return out


def _never_matches_hint(line: AttrLine) -> str | None:
    pattern = line.pattern
    if pattern.endswith("/"):
        # Verified against git 2.55: attributes are only ever looked up for
        # paths, and a pattern with a trailing slash can only match a
        # directory, so it matches nothing whatsoever. Not a stale path --
        # a pattern that could never have worked.
        return (
            "a trailing slash in gitattributes matches nothing at all, "
            f"not even inside the directory; {pattern.rstrip('/')}/** would"
        )
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
        for assignment in line.assignments:
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
                    message=f"{assignment.name} is overridden for all "
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
                if drivers.is_builtin(kind, name) or drivers.is_configured(kind, name, config_keys):
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
        if record.index_eol not in ("crlf", "mixed"):
            continue
        attrs = model.winners.get(record.path, {})
        line = attrs.get("eol") or attrs.get("text")
        if line is None:
            continue  # no rule claims this file, so no rule went unapplied
        resolved = _resolved_value(line, "eol") or _resolved_value(line, "text")
        if resolved in ("unset", "unspecified") or resolved == "crlf":
            continue  # the file is meant to be CRLF
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
                detail=shown[:10] + ([f"... and {len(shown) - 10} more"] if len(shown) > 10 else []),
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
        if line is None or _resolved_value(line, "filter") != "lfs":
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
                detail=shown[:10] + ([f"... and {len(shown) - 10} more"] if len(shown) > 10 else []),
            )
        )
    return out


def _resolved_value(line: AttrLine, name: str) -> str | None:
    for assignment in reversed(line.assignments):
        if assignment.name == name:
            return assignment.value
    return None


ALL_CHECKS = (
    "unparsable",
    "never-matches",
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
