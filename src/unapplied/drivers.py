"""Which diff, merge and filter drivers actually exist.

Naming a driver git has never heard of is silent. Verified against git 2.55:
`*.c diff=notarealdriver` produces no warning on any command, and git simply
uses the default behaviour -- so the attribute reads like a setting and is
a comment.
"""

from __future__ import annotations

#: Built-in userdiff drivers, from gitattributes(5) under "Defining a custom
#: hunk-header". Taken from git 2.55. CI cross-checks this list against the
#: installed man page, so a git that grows a driver fails the suite rather
#: than making the tool call a live driver dead.
BUILTIN_DIFF = frozenset(
    {
        "ada",
        "bash",
        "bibtex",
        "cpp",
        "csharp",
        "css",
        "dts",
        "elixir",
        "fortran",
        "fountain",
        "golang",
        "html",
        "java",
        "kotlin",
        "markdown",
        "matlab",
        "objc",
        "pascal",
        "perl",
        "php",
        "python",
        "ruby",
        "rust",
        "scheme",
        "tex",
    }
)

#: Merge drivers git implements itself. ll-merge.c.
BUILTIN_MERGE = frozenset({"text", "binary", "union"})

#: There are no built-in filter drivers. Even `lfs` is ordinary config that
#: `git lfs install` writes, which is why a fresh clone of an LFS repository
#: on a box without git-lfs quietly checks out pointer files.
BUILTIN_FILTER: frozenset[str] = frozenset()

_BUILTIN = {
    "diff": BUILTIN_DIFF,
    "merge": BUILTIN_MERGE,
    "filter": BUILTIN_FILTER,
}

#: Driver references that are genuinely undefined and deliberately left alone.
#:
#: `git lfs track` writes `filter=lfs diff=lfs merge=lfs -text`, and
#: `git lfs install` configures only `filter.lfs.*`. So `diff=lfs` and
#: `merge=lfs` really do name drivers that do not exist -- and reporting it
#: would fire on almost every repository using LFS, for a line the user did
#: not write and should not change. The `-text` beside them already settles
#: how git diffs and merges the file.
#:
#: `filter=lfs` is still reported when it is unconfigured, because that one
#: means LFS is not working.
CONVENTIONAL = frozenset({("diff", "lfs"), ("merge", "lfs")})


def is_conventional(kind: str, name: str) -> bool:
    return (kind, name) in CONVENTIONAL

#: A driver counts as configured if any of these subkeys is present.
_SUBKEYS = {
    "diff": ("command", "textconv", "xfuncname", "funcname", "binary", "cachetextconv", "wordregex", "algorithm", "external"),
    "merge": ("driver", "name", "recursive"),
    "filter": ("clean", "smudge", "process", "required"),
}


def is_builtin(kind: str, name: str) -> bool:
    return name in _BUILTIN.get(kind, frozenset())


def is_configured(kind: str, name: str, config_keys: set[str]) -> bool:
    """True if git config defines this driver anywhere it can see.

    Matching on the prefix rather than the known subkeys as well, because a
    driver configured with only an option this tool has not heard of is still
    plainly someone's deliberate driver, and calling it missing would be a
    false positive on a correct repository.
    """
    prefix = f"{kind}.{name.lower()}."
    return any(key.startswith(prefix) for key in config_keys)


def explain_missing(kind: str, name: str) -> str:
    if kind == "filter":
        if name == "lfs":
            return (
                "git-lfs is not configured here, so the filter is a no-op: "
                "`git lfs install` writes filter.lfs.* into config"
            )
        return "no filter.%s.clean, .smudge or .process in any config git can see" % name
    if kind == "diff":
        if name == "astextplain":
            # The Git for Windows gitattributes template. Its installer puts
            # diff.astextplain.textconv in the system config, so this line is
            # live on Windows and dead everywhere else -- which is worth
            # saying rather than suppressing, because it means a diff of these
            # files is not the same on two colleagues' machines.
            return (
                "Git for Windows defines diff.astextplain in its system config "
                "and nothing here does, so these files diff as plain binary on "
                "this box and as converted text on a Windows checkout"
            )
        return (
            "not a built-in driver and no diff.%s.* in any config git can see; "
            "git falls back to the default diff" % name
        )
    return (
        "not a built-in driver and no merge.%s.* in any config git can see; "
        "git falls back to the default merge" % name
    )
