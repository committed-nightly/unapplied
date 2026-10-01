"""The match resolution and the self-check that guards every finding."""

from __future__ import annotations

from pathlib import Path

from unapplied import attrfile, checks
from unapplied.gitrepo import Repo
from unapplied.match import resolve_matches


def model_for(path: Path):
    repo = Repo(path.resolve())
    tracked = repo.tracked_files()
    files = attrfile.discover(
        repo.root, repo.git_dir(), set(tracked), repo.config_get("core.attributesFile")
    )
    resolve_matches(files, tracked)
    return repo, files, checks.build_model(files, tracked)


def test_probe_reproduces_git_pattern_semantics(repo):
    """One repository exercising the corners, checked against git itself."""
    for rel in (
        "top.txt",
        "sub/mid.txt",
        "sub/deep/low.txt",
        "build/out.o",
        "vendor/dep/x.c",
        "has space.txt",
    ):
        repo.write(rel, "x\n")
    repo.write(
        ".gitattributes",
        "\n".join(
            [
                "*.txt a1",
                "/top.txt a2",
                "**/deep/** a3",
                "build/* a4",
                "sub/**/*.txt a5",
                "vendor/ a6",
                '"has space.txt" a7',
                "?op.txt a8",
                "*.[co] a9",
            ]
        )
        + "\n",
    )
    repo.commit()

    _, files, _ = model_for(repo.path)
    lines = files[0].lines
    matched = {line.pattern: sorted(line.matched) for line in lines}

    assert matched["/top.txt"] == ["top.txt"]
    assert matched["**/deep/**"] == ["sub/deep/low.txt"]
    assert matched["build/*"] == ["build/out.o"]
    assert matched["sub/**/*.txt"] == ["sub/deep/low.txt", "sub/mid.txt"]
    assert matched["vendor/"] == [], "trailing slash matches nothing"
    assert matched["has space.txt"] == ["has space.txt"]
    assert matched["?op.txt"] == ["top.txt"]
    assert matched["*.[co]"] == ["build/out.o", "vendor/dep/x.c"]
    assert "top.txt" in matched["*.txt"] and "sub/mid.txt" in matched["*.txt"]


def test_model_agrees_with_git_on_a_tangled_repository(repo):
    """The cross-check passing is only meaningful if the repo is hard."""
    repo.write("a.txt", "x\n")
    repo.write("sub/b.txt", "x\n")
    repo.write("sub/deep/c.txt", "x\n")
    repo.write(".gitattributes", "* text=auto\n*.txt eol=lf ident\n")
    repo.write("sub/.gitattributes", "*.txt eol=crlf\nb.txt -ident\n")
    repo.write("sub/deep/.gitattributes", "*.txt text\n")
    repo.commit()
    info = repo.path / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text("c.txt eol=lf\n", encoding="utf-8")

    repo_obj, _, model = model_for(repo.path)
    assert checks.disagreements(model, repo_obj) == []


def test_the_self_check_actually_catches_a_wrong_model(repo):
    """Prove the guard is not vacuous.

    Every finding is derived from the model, so the model being checked
    against git is the load-bearing claim of this tool. If corrupting the
    model does not trip the guard, the guard is decoration.
    """
    repo.write("a.txt", "x\n")
    repo.write(".gitattributes", "*.txt eol=lf\n*.txt eol=crlf\n")
    repo.commit()

    repo_obj, files, model = model_for(repo.path)
    assert checks.disagreements(model, repo_obj) == []

    # Invert the precedence rule: let the earlier line win.
    model.winners["a.txt"]["eol"] = files[0].lines[0]
    problems = checks.disagreements(model, repo_obj)
    assert problems, "corrupting the model did not trip the self-check"
    assert "a.txt" in problems[0]
    assert "eol" in problems[0]


def test_self_check_catches_a_missing_match(repo):
    repo.write("a.txt", "x\n")
    repo.write(".gitattributes", "*.txt text\n")
    repo.commit()

    repo_obj, files, model = model_for(repo.path)
    assert checks.disagreements(model, repo_obj) == []

    model.winners["a.txt"].pop("text")
    assert checks.disagreements(model, repo_obj)


def test_cli_refuses_to_report_on_a_broken_model(repo, monkeypatch, run):
    """A disagreement blames the tool, not the repository, and reports nothing."""
    repo.write("a.txt", "x\n")
    repo.write(".gitattributes", "*.rb text\n")
    repo.commit()

    monkeypatch.setattr(
        checks, "disagreements", lambda model, repo: ["a.txt: invented disagreement"]
    )
    code, out, err = run(repo.path)

    assert code == 2
    assert out == "", "findings were printed despite a model git disagrees with"
    assert "a bug in unapplied" in err
    assert "invented disagreement" in err


def test_later_line_wins_within_a_file(repo):
    repo.write("a.txt", "x\n")
    repo.write(".gitattributes", "*.txt k=first\n*.txt k=second\n")
    repo.commit()

    _, _, model = model_for(repo.path)
    assert model.winners["a.txt"]["k"].lineno == 2


def test_unspecified_clears_an_attribute(repo):
    """`!attr` is its own thing: not set, not unset, absent.

    `check-attr --all` omits an unspecified attribute entirely rather than
    printing "unspecified" -- you only see that word when you ask for the
    attribute by name. Both sides of the cross-check have to agree on that or
    every `!attr` in a repository would look like a disagreement.
    """
    repo.write("a.txt", "x\n")
    repo.write(".gitattributes", "*.txt ident\n*.txt !ident\n")
    repo.commit()

    repo_obj, _, model = model_for(repo.path)
    truth = repo_obj.check_attr_all(["a.txt"])
    assert truth["a.txt"] == {}, "check-attr --all started listing unspecified"
    assert checks.disagreements(model, repo_obj) == []

    # The `!ident` line genuinely beats the `ident` line, so saying so is right.
    found = checks.run_all(repo_obj, [], model, {"overridden"})
    assert [f.location for f in found] == [".gitattributes:1"]


def test_builtin_binary_macro_is_modelled(repo):
    """The bug the self-check caught on eight of sixteen real repositories."""
    repo.write("x.png", b"\x89PNG\r\n")
    repo.write(".gitattributes", "* text=auto\n*.png binary\n")
    repo.commit()

    repo_obj, _, model = model_for(repo.path)
    assert checks.disagreements(model, repo_obj) == []

    names = {a.name: a.value for a in model.assignments_of(model.winners["x.png"]["text"])}
    assert names["text"] == "unset"
    assert names["binary"] == "set"


def test_user_macro_is_modelled(repo):
    repo.write("x.q", "x\n")
    repo.write(".gitattributes", "[attr]mym text -diff\n*.q mym\n")
    repo.commit()

    repo_obj, _, model = model_for(repo.path)
    assert checks.disagreements(model, repo_obj) == []


def test_macro_defined_in_a_subdirectory_is_ignored_like_git_ignores_it(repo):
    repo.write("sub/f.c", "x\n")
    repo.write("sub/.gitattributes", "[attr]mym text\n*.c mym\n")
    repo.commit()

    repo_obj, _, model = model_for(repo.path)
    assert checks.disagreements(model, repo_obj) == []


def test_self_referencing_macro_does_not_hang(repo):
    repo.write("x.z", "x\n")
    repo.write(".gitattributes", "[attr]loop loop text\n*.z loop\n")
    repo.commit()

    repo_obj, _, model = model_for(repo.path)
    assert checks.disagreements(model, repo_obj) == []


def test_overridden_names_the_macro_it_came_from(repo):
    repo.write("x.png", b"\x89PNG\r\n")
    repo.write(".gitattributes", "*.png binary\n*.png text\n")
    repo.commit()

    repo_obj, _, model = model_for(repo.path)
    found = checks.run_all(repo_obj, [], model, {"overridden"})
    messages = " ".join(f.message for f in found)
    assert "via the binary macro" in messages


def test_eol_record_parses_an_attr_field_containing_a_space(repo):
    """`attr/text eol=crlf` is one field with a space in it.

    Splitting the line on whitespace loses the eol half, which made every
    `eol=crlf` file look like it had no rule at all.
    """
    repo.write("a.bat", b"x\r\n")
    repo.write(".gitattributes", "*.bat text eol=crlf\n")
    repo.commit()

    records = {r.path: r for r in Repo(repo.path.resolve()).ls_files_eol()}
    assert records["a.bat"].attr == "text eol=crlf"
    assert records[".gitattributes"].attr == ""


def test_eol_record_reports_binary_as_minus_text(repo):
    """Which is how a PNG under `* text=auto` escapes the unnormalized check."""
    repo.write("img.png", b"\x89PNG\r\n\x1a\n\x00\x00")
    repo.write(".gitattributes", "* text=auto\n")
    repo.commit()

    records = {r.path: r for r in Repo(repo.path.resolve()).ls_files_eol()}
    assert records["img.png"].index_eol == "-text"
    assert records["img.png"].attr == "text=auto"
