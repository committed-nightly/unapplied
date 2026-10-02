# unapplied

Find the `.gitattributes` rules git never applied — the ones that look like
settings and are comments.

If you have ever added `* text=auto` to a repository, watched `git status` stay
clean, and assumed that meant the line endings were fixed: they were not. The
attribute applies to the next `git add` of each file, not to what is already
committed. `git check-attr` will tell you `text: auto`, `git status` will tell
you nothing is wrong, and the blobs in your history will still be CRLF until
somebody runs `git add --renormalize .`. Nothing in git mentions it.

That is one of six things this looks for. Every one of them is something git
itself is silent about — if git already warns, it is not checked here, because
git's warning is better than anything this could add.

Written for the moment you inherit a repository with a forty-line
`.gitattributes` and no idea which of it is load-bearing.

## Install

Python 3.10 or newer, and git on your `PATH`.

```
pip install git+https://github.com/committed-nightly/unapplied
```

## Usage

```
unapplied [PATH] [--only CHECK] [--skip CHECK] [--json]
```

Exit status is `0` when nothing is unapplied, `1` when there are findings, `2`
on an error.

## A real example

`.gitattributes` in a repository where the files came first:

```
$ cat .gitattributes
* text=auto
*.psd filter=lfs -text

$ git check-attr --all windows.txt
windows.txt: text: auto           # git agrees the rule is in force
$ git status --porcelain          # says nothing about windows.txt or mixed.txt

$ unapplied .
.gitattributes:1: unnormalized: 2 file(s) this rule covers are still stored with CRLF in the index -- the rule was added after they were committed and `git add --renormalize .` was never run
    mixed.txt (mixed)
    windows.txt (crlf)
.gitattributes:2: lfs-not-pointer: 1 file(s) this rule covers are stored as their own contents, not as LFS pointers -- the rule was added after they were committed, so LFS never took them
    asset.psd
```

The second one is the expensive mistake: everything reports LFS as configured,
and the fat blob is in your history anyway.

What `git status` prints for `asset.psd` depends on the machine, which is why
the example above only claims the CRLF half. With git-lfs installed and
`filter.lfs.clean` configured — likely, if you are the sort of person who
reaches for this — `git status` applies the clean filter to the worktree file,
gets a pointer, compares it to the fat blob in the index and reports
` M asset.psd`. Without git-lfs it is silent and the status is fully clean.
Neither answer tells you LFS never took the file: one says nothing, and the
other says you have an uncommitted change to a file you have not touched.

## The checks

| check | what it finds |
| --- | --- |
| `unnormalized` | blobs still stored with CRLF under a rule that says the file is text |
| `lfs-not-pointer` | blobs stored as themselves under `filter=lfs`, so LFS never took them |
| `never-matches` | a pattern matching no tracked path at all — a stale path or a typo |
| `directory-only` | per-file attributes on a pattern that only ever matches a directory |
| `overridden` | a line whose attribute a later line overrides for every file it matches |
| `undefined-driver` | `diff=`, `merge=` or `filter=` naming a driver no config defines |
| `unparsable` | a line git rejects entirely, applying none of its attributes |

`unparsable` is in the list and not in the table above it because it is the dull
one.

### On `directory-only`

`/Tests export-ignore` matches no file, and it works: `git archive` asks about
directories while walking the tree and drops the whole subtree. `Tests/ -text`
matches the same nothing and does not work, because git only ever asks a *file*
what its line endings are. So a directory pattern is only reported when it
carries an attribute about file content, and `export-ignore` on one is left
alone.

This distinction is the reason `never-matches` is quiet on Symfony, which has
211 `.gitattributes` files almost entirely made of `/Tests export-ignore`.

## How it decides

It does not implement gitattributes pattern matching. Wildmatch has enough
corners — `**`, basename versus anchored, a trailing slash that matches only
the slashed spelling — that a reimplementation would just be a second thing to
be wrong about.

Instead it builds a scratch repository holding the same attributes files, in
the same directories, with every pattern exactly as written and every attribute
replaced by one unique sentinel per source line. `git check-attr` in that
repository then names the sentinel of every line that matched a path. Unique
sentinels mean no line can override another, so the answer is the full match
set rather than just the winner. Git does the matching.

Precedence, later-line-wins and macro expansion *are* modelled here, so before
printing anything the tool resolves every tracked file's attributes from its own
model and compares that to `git check-attr --all` on your actual repository. If
the two disagree it reports its own bug and exits without findings, rather than
telling you something confident and wrong:

```
unapplied: this tool and git disagree about what these attributes resolve to,
so no findings are reported -- this is a bug in unapplied, not in your
repository.
```

That guard found two real bugs during development, on 8 of the 16 repositories
it was first run against: the built-in `binary` macro was not being expanded,
and directory patterns were being called dead.

## What it will not tell you

- **Macros defined outside the top-level file.** Git already rejects those by
  name and line number.
- **`diff=lfs` and `merge=lfs`.** `git lfs track` writes them, `git lfs
  install` defines neither, and the `-text` beside them already settles how git
  diffs and merges the file. `filter=lfs` *is* reported when unconfigured,
  because that one means LFS is not working.
- **Whether an attribute means anything to another tool.** `linguist-generated`
  on a directory is left alone; GitHub's business, not git's.

Driver findings depend on the config of the box you run on, by design — a
driver defined only in your colleague's `~/.gitconfig` is dead for you, and
that is the thing worth knowing. `diff=astextplain` says so explicitly, since
Git for Windows defines it and other platforms do not.

## Tests

```
pip install -e ".[dev]"
python -m pytest
```

`tests/test_git_behaviour.py` asserts the claims this tool makes about git
against the git you have installed, including cross-checking the built-in
diff driver list against your `gitattributes(5)`. If a future git changes its
mind, those fail first and loudly.

## Licence

MIT.
