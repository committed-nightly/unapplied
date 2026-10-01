"""Printing findings."""

from __future__ import annotations

import json
from typing import Iterable, TextIO

from .checks import Finding


def text(findings: Iterable[Finding], out: TextIO, scanned: int, sources: list[str]) -> None:
    findings = list(findings)
    if not findings:
        where = ", ".join(sources) if sources else "no gitattributes files"
        print(f"unapplied: nothing unapplied in {where} ({scanned} tracked files)", file=out)
        return

    for finding in findings:
        print(f"{finding.location}: {finding.check}: {finding.message}", file=out)
        for line in finding.detail:
            print(f"    {line}", file=out)

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.check] = counts.get(finding.check, 0) + 1
    summary = ", ".join(f"{n} {check}" for check, n in sorted(counts.items()))
    print(f"\n{len(findings)} finding(s): {summary}", file=out)


def as_json(findings: Iterable[Finding], out: TextIO, scanned: int, sources: list[str]) -> None:
    payload = {
        "tracked_files": scanned,
        "sources": sources,
        "findings": [
            {
                "check": f.check,
                "location": f.location,
                "message": f.message,
                "detail": f.detail,
            }
            for f in findings
        ],
    }
    json.dump(payload, out, indent=2)
    print(file=out)
