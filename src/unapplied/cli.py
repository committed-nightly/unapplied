"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import attrfile, checks, report
from .gitrepo import BadPath, GitFailed, NotARepository, Repo
from .match import ProbeFailed, resolve_matches

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unapplied",
        description="Find the .gitattributes rules git never applied.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="a path inside the repository to check (default: the current directory)",
    )
    parser.add_argument(
        "--only",
        metavar="CHECK",
        action="append",
        default=[],
        help="run only this check; repeatable. One of: " + ", ".join(checks.ALL_CHECKS),
    )
    parser.add_argument(
        "--skip",
        metavar="CHECK",
        action="append",
        default=[],
        help="do not run this check; repeatable",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--list-checks",
        action="store_true",
        help="print the check names and exit",
    )
    return parser


class BadUsage(Exception):
    """Arguments that parse but name something that does not exist."""


def _enabled(only: list[str], skip: list[str]) -> set[str]:
    unknown = [c for c in (*only, *skip) if c not in checks.ALL_CHECKS]
    if unknown:
        raise BadUsage(
            f"unapplied: no such check: {', '.join(unknown)}\n"
            f"           known checks: {', '.join(checks.ALL_CHECKS)}"
        )
    enabled = set(only) if only else set(checks.ALL_CHECKS)
    return enabled - set(skip)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_checks:
        for check in checks.ALL_CHECKS:
            print(check)
        return EXIT_CLEAN

    # Raised SystemExit with a message before, which argparse-style looks fine
    # and exits 1 -- the code the README reserves for findings.
    try:
        enabled = _enabled(args.only, args.skip)
    except BadUsage as exc:
        print(exc, file=sys.stderr)
        return EXIT_ERROR

    try:
        repo = Repo(Path(args.path).resolve())
    except (BadPath, NotARepository, GitFailed) as exc:
        print(f"unapplied: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        tracked = repo.tracked_files()
        files = attrfile.discover(
            repo.root,
            repo.git_dir(),
            set(tracked),
            repo.config_get("core.attributesFile"),
        )
    except GitFailed as exc:
        print(f"unapplied: {exc}", file=sys.stderr)
        return EXIT_ERROR

    sources = [f.source for f in files]
    if not files:
        print(
            "unapplied: no gitattributes files in this repository, so there is "
            "nothing that could go unapplied",
            file=sys.stderr,
        )
        return EXIT_CLEAN

    ignorecase = (repo.config_get("core.ignorecase") or "false").lower() == "true"
    try:
        resolve_matches(files, tracked, ignorecase=ignorecase)
    except ProbeFailed as exc:
        print(f"unapplied: {exc}", file=sys.stderr)
        return EXIT_ERROR

    model = checks.build_model(files, tracked)

    problems = checks.disagreements(model, repo)
    if problems:
        # Refusing rather than guessing. Every finding is derived from this
        # model, so if the model and git disagree about what the attributes
        # resolve to, the findings cannot be trusted and should not be shown.
        print(
            "unapplied: this tool and git disagree about what these attributes "
            "resolve to, so no findings are reported -- this is a bug in "
            "unapplied, not in your repository. Please report it with the lines "
            "below:",
            file=sys.stderr,
        )
        for problem in problems[:20]:
            print(f"  {problem}", file=sys.stderr)
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more", file=sys.stderr)
        return EXIT_ERROR

    if not repo.has_commits():
        # unnormalized and lfs-not-pointer read the index, which exists, but
        # saying so is kinder than silently checking less than advertised.
        print(
            "unapplied: this repository has no commits yet; checking the index only",
            file=sys.stderr,
        )

    findings = checks.run_all(repo, files, model, enabled)

    if args.json:
        report.as_json(findings, sys.stdout, len(tracked), sources)
    else:
        report.text(findings, sys.stdout, len(tracked), sources)

    return EXIT_FINDINGS if findings else EXIT_CLEAN


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
