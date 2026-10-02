"""Each check, on a repository built to earn exactly that finding."""

from __future__ import annotations

from pathlib import Path

from unapplied import attrfile, checks
from unapplied.gitrepo import Repo
from unapplied.match import resolve_matches


def analyse(path: Path, enabled: set[str] | None = None) -> list[checks.Finding]:
    repo = Repo(path.resolve())
    tracked = repo.tracked_files()
    files = attrfile.discover(
        repo.root, repo.git_dir(), set(tracked), repo.config_get("core.attributesFile")
    )
    resolve_matches(files, tracked)
    model = checks.build_model(files, tracked)
    assert checks.disagreements(model, repo) == [], (
        "the model disagrees with git check-attr, so this test is not testing "
        "what it thinks it is"
    )
    return checks.run_all(repo, files, model, enabled or set(checks.ALL_CHECKS))


def kinds(findings: list[checks.Finding]) -> list[str]:
    return [f.check for f in findings]


def at(findings: list[checks.Finding], location: str) -> list[checks.Finding]:
    return [f for f in findings if f.location == location]


# --- never-matches -----------------------------------------------------------


def test_pattern_matching_no_tracked_file(repo):
    repo.write("a.py", "x\n")
    repo.write(".gitattributes", "*.py text\n*.rb text\n")
    repo.commit()

    found = analyse(repo.path)
    assert kinds(found) == ["never-matches"]
    assert found[0].location == ".gitattributes:2"
    assert "'*.rb'" in found[0].message


def test_a_directory_pattern_is_not_never_matches(repo):
    """It matches the directory, which is a real thing for export-ignore."""
    repo.write("vendor/dep.c", "x\n")
    repo.write(".gitattributes", "vendor/ export-ignore\nTests export-ignore\n")
    repo.write("Tests/t.c", "x\n")
    repo.commit()

    assert analyse(repo.path) == []


def test_export_ignore_on_a_directory_really_works(repo):
    """The reason the above is not a finding, asserted against git archive."""
    repo.write("vendor/dep.c", "x\n")
    repo.write("keep.c", "x\n")
    repo.write(".gitattributes", "vendor/ export-ignore\n")
    repo.commit()

    archive = repo.git("archive", "HEAD", "--format=tar")
    assert "keep.c" in archive
    assert "vendor/dep.c" not in archive, (
        "git archive stopped honouring export-ignore on a trailing-slash "
        "directory pattern, so such a line is dead after all"
    )


# --- directory-only ----------------------------------------------------------


def test_per_file_attribute_on_a_directory_pattern(repo):
    repo.write("vendor/dep.c", "x\n")
    repo.write(".gitattributes", "vendor/ -text\n")
    repo.commit()

    found = analyse(repo.path, {"directory-only"})
    assert len(found) == 1
    assert "text" in found[0].message
    assert "vendor/**" in found[0].message
    assert "1 directory" in found[0].message


def test_directory_pattern_with_only_export_ignore_is_clean(repo):
    repo.write("Tests/t.c", "x\n")
    repo.write(".gitattributes", "/Tests export-ignore\n")
    repo.commit()

    assert analyse(repo.path, {"directory-only"}) == []


def test_directory_pattern_mixing_both_reports_only_the_dead_half(repo):
    repo.write("Tests/t.c", "x\n")
    repo.write(".gitattributes", "/Tests export-ignore -text\n")
    repo.commit()

    found = analyse(repo.path, {"directory-only"})
    assert len(found) == 1
    assert "text" in found[0].message
    assert "export-ignore" not in found[0].message


def test_unknown_attribute_on_a_directory_is_not_reported(repo):
    """A third-party tool may well ask about directories; git's opinion on
    linguist-vendored is not a reason to call the line dead."""
    repo.write("vendor/dep.c", "x\n")
    repo.write(".gitattributes", "vendor/ linguist-vendored\n")
    repo.commit()

    assert analyse(repo.path, {"directory-only"}) == []


def test_pattern_matching_both_files_and_directories_is_clean(repo):
    """One match on a real file is enough; the rule is doing something."""
    repo.write("thing.c", "x\n")
    repo.write("things/inner.c", "x\n")
    repo.write(".gitattributes", "thing* -text\n")
    repo.commit()

    found = analyse(repo.path, {"directory-only"})
    assert found == [], "a pattern matching thing.c was called directory-only"


def test_a_pattern_matching_only_a_directory_by_accident(repo):
    """`src*` does not match src/a.c: no slash in the pattern means it is
    matched against the basename, and `*` does not cross a slash. So it
    catches the directory and nothing else."""
    repo.write("src/a.c", "x\n")
    repo.write(".gitattributes", "src* -text\n")
    repo.commit()

    found = analyse(repo.path, {"directory-only"})
    assert len(found) == 1
    assert "matched: src" in found[0].detail


def test_untracked_file_does_not_rescue_a_pattern(repo):
    """Attributes only ever apply to tracked paths, so only those count."""
    repo.write("a.py", "x\n")
    repo.commit()
    repo.write("ignored.rb", "x\n")  # present on disk, never added

    found = analyse(repo.path, {"never-matches"})
    assert found == []

    repo.write(".gitattributes", "*.rb text\n")
    repo.commit_only("attrs", ".gitattributes")
    found = analyse(repo.path, {"never-matches"})
    assert kinds(found) == ["never-matches"]


def test_pattern_in_subdirectory_file_is_relative_to_it(repo):
    """The classic: repeating the directory inside its own attributes file."""
    repo.write("sub/f.c", "x\n")
    repo.write("sub/.gitattributes", "sub/f.c text\n*.c text\n")
    repo.commit()

    found = analyse(repo.path, {"never-matches"})
    assert len(found) == 1
    assert found[0].location == "sub/.gitattributes:1"
    assert "relative to sub/.gitattributes" in found[0].message
    assert "sub/sub/f.c" in found[0].message


def test_a_matching_pattern_is_not_reported(repo):
    repo.write("deep/nested/f.py", "x\n")
    repo.write(".gitattributes", "*.py text\n**/nested/** text\n/deep/nested/f.py text\n")
    repo.commit()
    assert analyse(repo.path, {"never-matches"}) == []


# --- overridden --------------------------------------------------------------


def test_later_line_in_the_same_file_wins(repo):
    repo.write("a.txt", "x\n")
    repo.write(".gitattributes", "*.txt eol=lf\n*.txt eol=crlf\n")
    repo.commit()

    found = analyse(repo.path, {"overridden"})
    assert len(found) == 1
    assert found[0].location == ".gitattributes:1"
    assert ".gitattributes:2" in found[0].message


def test_deeper_attributes_file_wins(repo):
    repo.write("sub/a.txt", "x\n")
    repo.write(".gitattributes", "*.txt eol=lf\n")
    repo.write("sub/.gitattributes", "*.txt eol=crlf\n")
    repo.commit()

    found = analyse(repo.path, {"overridden"})
    assert len(found) == 1
    assert found[0].location == ".gitattributes:1"
    assert "sub/.gitattributes:1" in found[0].message


def test_line_that_still_wins_somewhere_is_not_reported(repo):
    """Overriding one of two matches does not make the line dead."""
    repo.write("top.txt", "x\n")
    repo.write("sub/deep.txt", "x\n")
    repo.write(".gitattributes", "*.txt eol=lf\n")
    repo.write("sub/.gitattributes", "*.txt eol=crlf\n")
    repo.commit()

    assert analyse(repo.path, {"overridden"}) == []


def test_only_the_overridden_attribute_is_reported(repo):
    """A line setting two attributes, one of which survives."""
    repo.write("a.txt", "x\n")
    repo.write(".gitattributes", "*.txt eol=lf ident\n*.txt eol=crlf\n")
    repo.commit()

    found = analyse(repo.path, {"overridden"})
    assert len(found) == 1
    assert "eol is overridden" in found[0].message


def test_identical_duplicate_line_reports_the_earlier_one(repo):
    repo.write("a.txt", "x\n")
    repo.write(".gitattributes", "*.txt text\n*.txt text\n")
    repo.commit()

    found = analyse(repo.path, {"overridden"})
    assert [f.location for f in found] == [".gitattributes:1"]


# --- undefined-driver --------------------------------------------------------


def test_unknown_diff_driver(repo):
    repo.write("f.kt", "x\n")
    repo.write(".gitattributes", "*.kt diff=nosuchdriver\n")
    repo.commit()

    found = analyse(repo.path, {"undefined-driver"})
    assert len(found) == 1
    assert "diff=nosuchdriver" in found[0].message


def test_builtin_diff_driver_is_fine(repo):
    repo.write("f.rs", "x\n")
    repo.write("g.py", "x\n")
    repo.write(".gitattributes", "*.rs diff=rust\n*.py diff=python\n")
    repo.commit()
    assert analyse(repo.path, {"undefined-driver"}) == []


def test_driver_defined_in_repo_config_is_fine(repo):
    repo.write("f.dat", "x\n")
    repo.write(".gitattributes", "*.dat diff=mydat\n")
    repo.commit()
    repo.git("config", "diff.mydat.textconv", "xxd")

    assert analyse(repo.path, {"undefined-driver"}) == []


def test_driver_defined_with_an_unknown_subkey_is_still_fine(repo):
    """Deliberately lenient: a driver configured at all is someone's choice."""
    repo.write("f.dat", "x\n")
    repo.write(".gitattributes", "*.dat diff=mydat\n")
    repo.commit()
    repo.git("config", "diff.mydat.somethingneworunknown", "1")

    assert analyse(repo.path, {"undefined-driver"}) == []


def test_builtin_merge_drivers_are_fine(repo):
    repo.write("CHANGELOG.md", "x\n")
    repo.write(".gitattributes", "CHANGELOG.md merge=union\n")
    repo.commit()
    assert analyse(repo.path, {"undefined-driver"}) == []


def test_unconfigured_lfs_filter_says_so(repo):
    """Driver resolution against an explicit config set.

    Not via analyse(): this box has git-lfs installed globally, so a test
    that read real config would pass without proving anything.
    """
    repo.write("f.psd", "x\n")
    repo.write(".gitattributes", "*.psd filter=lfs\n")
    repo.commit()

    files = attrfile.discover(repo.path, repo.path / ".git", {".gitattributes"}, None)

    found = checks.check_drivers(files, config_keys=set())
    assert len(found) == 1
    assert "git lfs install" in found[0].message

    found = checks.check_drivers(files, config_keys={"filter.lfs.clean"})
    assert found == []


def test_lfs_filter_is_fine_when_lfs_is_installed_on_this_box(repo):
    """Which it is, in CI and on most developer machines."""
    repo.write("f.psd", "x\n")
    repo.write(".gitattributes", "*.psd filter=lfs\n")
    repo.commit()

    configured = any(
        k.startswith("filter.lfs.") for k in Repo(repo.path.resolve()).config_keys()
    )
    found = analyse(repo.path, {"undefined-driver"})
    assert (found == []) is configured


def test_plain_set_diff_attribute_is_not_a_driver_reference(repo):
    """`diff` and `-diff` say whether to diff at all, not which driver."""
    repo.write("f.bin", "x\n")
    repo.write(".gitattributes", "*.bin -diff\n")
    repo.commit()
    assert analyse(repo.path, {"undefined-driver"}) == []


# --- unnormalized ------------------------------------------------------------


def test_crlf_blob_under_a_text_rule(repo):
    repo.write("win.txt", b"a\r\nb\r\n")
    repo.write("unix.txt", b"a\nb\n")
    repo.commit()
    repo.write(".gitattributes", "* text=auto\n")
    repo.commit_only("attrs", ".gitattributes")

    found = analyse(repo.path, {"unnormalized"})
    assert len(found) == 1
    assert found[0].location == ".gitattributes:1"
    assert any("win.txt" in d for d in found[0].detail)
    assert not any("unix.txt" in d for d in found[0].detail)
    assert "renormalize" in found[0].message


def test_renormalized_repository_is_clean(repo):
    repo.write("win.txt", b"a\r\nb\r\n")
    repo.commit()
    repo.write(".gitattributes", "* text=auto\n")
    repo.commit_only("attrs", ".gitattributes")
    assert analyse(repo.path, {"unnormalized"})  # the finding exists first

    repo.git("add", "--renormalize", ".")
    repo.git("commit", "-q", "-m", "renormalize")
    assert analyse(repo.path, {"unnormalized"}) == []


def test_eol_crlf_still_wants_lf_in_the_index(repo):
    """`eol=crlf` converts on checkout; the blob is still meant to be LF.

    Asserted against git first, because the obvious reading of `eol=crlf` is
    that a CRLF blob is what it asked for, and that reading is wrong.
    """
    repo.write("script.bat", b"echo\r\n")
    repo.commit()
    repo.write(".gitattributes", "*.bat text eol=crlf\n")
    repo.commit_only("attrs", ".gitattributes")

    found = analyse(repo.path, {"unnormalized"})
    assert len(found) == 1, (
        "git stores LF in the index under eol=crlf, so a CRLF blob is a finding"
    )

    repo.git("add", "--renormalize", ".")
    repo.git("commit", "-q", "-m", "renormalize")
    assert "i/lf" in repo.git("ls-files", "--eol", "--", "script.bat")
    assert analyse(repo.path, {"unnormalized"}) == []


def test_minus_text_file_is_not_reported(repo):
    """`-text` is the one case where a CRLF blob is correct."""
    repo.write("fixture.txt", b"a\r\nb\r\n")
    repo.commit()
    repo.write(".gitattributes", "* text=auto eol=lf\nfixture.txt -text\n")
    repo.commit_only("attrs", ".gitattributes")

    assert analyse(repo.path, {"unnormalized"}) == []


def test_inert_eol_next_to_minus_text_is_not_reported(repo):
    """The godot false positive: -text wins, and the eol beside it does nothing.

    `* text=auto eol=lf` plus a later `-text` resolves to `text: unset` with
    `eol: lf` still showing in check-attr. Reading the eol winner alone makes
    this look unnormalised; git does no conversion at all.
    """
    repo.write("tests/line_endings_crlf.txt", b"a\r\nb\r\n")
    repo.commit()
    repo.write(
        ".gitattributes",
        "* text=auto eol=lf\n*_crlf.txt -text\n",
    )
    repo.commit_only("attrs", ".gitattributes")

    repo_obj = Repo(repo.path.resolve())
    assert "-text" in repo_obj.ls_files_eol()[-1].attr
    assert analyse(repo.path, {"unnormalized"}) == []


def test_binary_file_with_crlf_bytes_is_not_reported(repo):
    """-text means git was told to leave it alone."""
    repo.write("blob.bin", b"\x00\x01a\r\nb\r\n")
    repo.commit()
    repo.write(".gitattributes", "*.bin -text\n")
    repo.commit_only("attrs", ".gitattributes")

    assert analyse(repo.path, {"unnormalized"}) == []


def test_crlf_file_with_no_rule_covering_it_is_not_reported(repo):
    """Nothing went unapplied if nothing was ever applied."""
    repo.write("win.txt", b"a\r\nb\r\n")
    repo.write(".gitattributes", "*.md text\n")
    repo.write("readme.md", "x\n")
    repo.commit()

    assert analyse(repo.path, {"unnormalized"}) == []


def test_mixed_endings_count(repo):
    repo.write("mix.txt", b"a\r\nb\n")
    repo.commit()
    repo.write(".gitattributes", "* text=auto\n")
    repo.commit_only("attrs", ".gitattributes")

    found = analyse(repo.path, {"unnormalized"})
    assert len(found) == 1
    assert any("(mixed)" in d for d in found[0].detail)


# --- lfs-not-pointer ---------------------------------------------------------


def test_lfs_rule_over_a_real_blob(repo):
    repo.write("asset.psd", "the actual bytes, not a pointer\n")
    repo.commit()
    repo.write(".gitattributes", "*.psd filter=lfs -text\n")
    repo.commit_only("attrs", ".gitattributes")

    found = analyse(repo.path, {"lfs-not-pointer"})
    assert len(found) == 1
    assert found[0].location == ".gitattributes:1"
    assert found[0].detail == ["asset.psd"]


def test_actual_pointer_is_not_reported(repo):
    repo.write(
        "asset.psd",
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "0" * 64 + "\nsize 12\n",
    )
    repo.write(".gitattributes", "*.psd filter=lfs -text\n")
    repo.commit()

    assert analyse(repo.path, {"lfs-not-pointer"}) == []


def test_non_lfs_filter_is_not_checked_for_pointers(repo):
    repo.write("f.dat", "content\n")
    repo.write(".gitattributes", "*.dat filter=myfilter\n")
    repo.commit()

    assert analyse(repo.path, {"lfs-not-pointer"}) == []


# --- unparsable --------------------------------------------------------------


def test_invalid_attribute_name_reports_the_line(repo):
    repo.write("f.b", "x\n")
    repo.write(".gitattributes", "*.b goodattr =noname\n")
    repo.commit()

    found = analyse(repo.path, {"unparsable"})
    assert len(found) == 1
    assert found[0].location == ".gitattributes:1"


def test_pattern_with_no_attributes(repo):
    repo.write("f.b", "x\n")
    repo.write(".gitattributes", "*.b\n")
    repo.commit()

    found = analyse(repo.path, {"unparsable"})
    assert len(found) == 1
    assert "no attributes" in found[0].message


def test_comments_and_blanks_are_not_findings(repo):
    repo.write("f.py", "x\n")
    repo.write(".gitattributes", "# a comment\n\n   \n*.py text\n")
    repo.commit()

    assert analyse(repo.path) == []


def test_quoted_pattern_with_a_space(repo):
    repo.write("has space.txt", "x\n")
    repo.write(".gitattributes", '"has space.txt" text\n')
    repo.commit()

    assert analyse(repo.path) == []


def test_quoted_pattern_that_matches_nothing_is_still_reported(repo):
    repo.write("other.txt", "x\n")
    repo.write(".gitattributes", '"no such file.txt" text\n')
    repo.commit()

    found = analyse(repo.path, {"never-matches"})
    assert len(found) == 1
    assert "no such file.txt" in found[0].message


def test_the_lfs_track_boilerplate_is_not_reported(repo):
    """`git lfs track` writes diff=lfs and merge=lfs, which git-lfs never
    defines. True, universal, and not worth telling anyone about."""
    repo.write("f.sqlite", "x\n")
    repo.write(".gitattributes", "*.sqlite filter=lfs diff=lfs merge=lfs -text\n")
    repo.commit()

    files = attrfile.discover(repo.path, repo.path / ".git", {".gitattributes"}, None)
    found = checks.check_drivers(files, config_keys={"filter.lfs.clean"})
    assert found == []

    # filter=lfs is still reported when LFS itself is not configured.
    found = checks.check_drivers(files, config_keys=set())
    assert [f.check for f in found] == ["undefined-driver"]
    assert "git lfs install" in found[0].message
