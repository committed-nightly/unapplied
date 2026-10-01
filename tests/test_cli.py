"""The command line: exit codes, filtering, and the things it refuses to do."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from unapplied.cli import EXIT_CLEAN, EXIT_ERROR, EXIT_FINDINGS, main


def test_clean_repository_exits_zero(repo, run):
    repo.write("a.py", "x\n")
    repo.write(".gitattributes", "*.py text\n")
    repo.commit()

    code, out, _ = run(repo.path)
    assert code == EXIT_CLEAN
    assert "nothing unapplied" in out


def test_findings_exit_one(repo, run):
    repo.write("a.py", "x\n")
    repo.write(".gitattributes", "*.rb text\n")
    repo.commit()

    code, out, _ = run(repo.path)
    assert code == EXIT_FINDINGS
    assert "never-matches" in out
    assert "1 finding(s)" in out


def test_no_attributes_file_is_not_a_failure(repo, run):
    repo.write("a.py", "x\n")
    repo.commit()

    code, out, err = run(repo.path)
    assert code == EXIT_CLEAN
    assert "nothing that could go unapplied" in err
    assert out == ""


def test_not_a_repository(tmp_path, run):
    target = tmp_path / "plain"
    target.mkdir()

    code, _, err = run(target)
    assert code == EXIT_ERROR
    assert "not inside a git repository" in err


def test_only_filters_to_one_check(repo, run):
    repo.write("a.py", "x\n")
    repo.write(".gitattributes", "*.rb diff=nosuchdriver\n")
    repo.commit()

    code, out, _ = run(repo.path, "--only", "undefined-driver")
    assert code == EXIT_FINDINGS
    assert "undefined-driver" in out
    assert "never-matches" not in out


def test_skip_removes_a_check(repo, run):
    repo.write("a.py", "x\n")
    repo.write(".gitattributes", "*.rb diff=nosuchdriver\n")
    repo.commit()

    code, out, _ = run(repo.path, "--skip", "never-matches")
    assert "never-matches" not in out
    assert "undefined-driver" in out


def test_skipping_everything_exits_clean(repo, run):
    repo.write("a.py", "x\n")
    repo.write(".gitattributes", "*.rb text\n")
    repo.commit()

    args = []
    for check in ("unparsable", "never-matches", "overridden", "undefined-driver", "unnormalized", "lfs-not-pointer"):
        args += ["--skip", check]
    code, out, _ = run(repo.path, *args)
    assert code == EXIT_CLEAN


def test_unknown_check_name_is_rejected(repo):
    repo.write("a.py", "x\n")
    repo.commit()

    with pytest.raises(SystemExit) as excinfo:
        main([str(repo.path), "--only", "nosuchcheck"])
    assert "no such check" in str(excinfo.value)


def test_json_output(repo, run):
    repo.write("a.py", "x\n")
    repo.write(".gitattributes", "*.rb text\n")
    repo.commit()

    code, out, _ = run(repo.path, "--json")
    assert code == EXIT_FINDINGS
    payload = json.loads(out)
    assert payload["tracked_files"] == 2
    assert payload["sources"] == [".gitattributes"]
    assert payload["findings"][0]["check"] == "never-matches"
    assert payload["findings"][0]["location"] == ".gitattributes:1"


def test_json_is_valid_when_clean(repo, run):
    repo.write("a.py", "x\n")
    repo.write(".gitattributes", "*.py text\n")
    repo.commit()

    code, out, _ = run(repo.path, "--json")
    assert code == EXIT_CLEAN
    assert json.loads(out)["findings"] == []


def test_runs_from_a_subdirectory(repo, run):
    repo.write("sub/a.py", "x\n")
    repo.write(".gitattributes", "*.rb text\n")
    repo.commit()

    code, out, _ = run(repo.path / "sub")
    assert code == EXIT_FINDINGS
    # Findings are reported against the repository root regardless of cwd.
    assert ".gitattributes:1" in out


def test_repository_with_no_commits(repo, run):
    repo.write("a.py", "x\n")
    repo.write(".gitattributes", "*.rb text\n")
    repo.git("add", "-A")

    code, out, err = run(repo.path)
    assert code == EXIT_FINDINGS
    assert "no commits yet" in err


def test_list_checks():
    assert main(["--list-checks"]) == EXIT_CLEAN


def test_installed_as_a_command(repo):
    """It is a CLI; at least once, run it as one."""
    repo.write("a.py", "x\n")
    repo.write(".gitattributes", "*.rb text\n")
    repo.commit()

    proc = subprocess.run(
        ["unapplied", str(repo.path)], capture_output=True, text=True
    )
    assert proc.returncode == EXIT_FINDINGS
    assert "never-matches" in proc.stdout


def test_path_with_a_newline_in_it(repo, run):
    """NUL-separated plumbing, so this should just work."""
    repo.write("odd\nname.py", "x\n")
    repo.write(".gitattributes", "*.py text\n*.rb text\n")
    repo.commit()

    code, out, _ = run(repo.path)
    assert code == EXIT_FINDINGS
    assert "'*.rb'" in out
    assert "'*.py'" not in out, "the newline in the path broke match resolution"
