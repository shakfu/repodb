# repodb

Keep a SQLite database of git project clone URLs. Clone, compare and change the projects in bulk.

Each project is stored as `(owner, name, url)`, keyed by url: https and ssh urls of one repo are one project. The name is the clone directory, and `OWNER/NAME` is unique too. The owner is the first path component after the host (`alice` in `github.com/alice/r` or `git@gitlab.com:alice/r`), for any host.

## Install

```sh
pip install repodb
```

This installs `repodb`. It needs Python 3.10 or later, `git`, and Linux or macOS. `github` and `publish` without `--push-default` also need [`gh`](https://cli.github.com).

## Usage

### Add and remove projects

```sh
repodb scan ~/src                         # add origin URLs of local projects
repodb github USER [--source] [--ssh]     # add USER's GitHub repos and topics (needs gh)
repodb github --refresh [USER]            # update topics of stored GitHub repos; add none
repodb github --refresh --prune [--yes]   # list, then remove, repos gh no longer lists
repodb import projects.json
repodb remove OWNER/NAME [NAME ...] | --owner USER --all
```

### Select projects

`list`, `export`, `clone`, `status` and `apply` take the same filters. A project must match all of them.

```sh
--owner USER            # one owner's projects
-s SET                  # a named set (see below)
-t TOPIC [-t ...]       # every topic given; with --any, at least one
```

```sh
repodb list [-g] [-u | --slugs] [--topics]  # names, URLs, or OWNER/REPO
repodb topics [--owner USER]              # each topic and its project count
repodb set --name SET SPEC ...            # add projects to a set
repodb set [--name SET [--remove SPEC ... | --delete]]  # list or change sets
repodb export [-g] -o projects.json
repodb info                               # format, counts, hosts, topics; read-only
```

- A SPEC is `OWNER/NAME`, or `NAME` if only one owner has it.

- A set is a local, named selection, unrelated to topics. Adding to a set is idempotent and all-or-nothing.

- Topics come from `github` and `import`.

- `list --slugs` prints `OWNER/REPO` from each url, for tools such as [git-xargs](https://github.com/gruntwork-io/git-xargs) or [multi-gitter](https://github.com/lindell/multi-gitter).

### Clone and compare

```sh
repodb clone DEST [-g] [-j N]             # selected projects into DEST/NAME or DEST/OWNER/NAME
repodb clone DEST -r OWNER/NAME [-r ...]  # named projects
repodb clone DEST --json projects.json [-t TOPIC]
repodb clone DEST -- --depth 1            # options after -- go to git clone
repodb status DEST [-g]                   # compare DEST with the database
```

- `clone` skips targets that exist. `-j N` runs N clones at once.

- `status` prints `missing`, `untracked`, `differs`, `not a git repo` or `no origin` per project, and exits 1 if it prints anything.

### Change many repos

`apply` prepares one change across many repos as local commits. `publish` pushes them. Nothing leaves your machine until `publish`.

```sh
repodb template py > fix.py                                   # 1. write the change
repodb apply bump-ci -s agents --script fix.py -m "Bump CI"   # 2. commit it locally
repodb review bump-ci --diff                                  # 3. inspect
repodb publish bump-ci                                        # 4. open a PR per repo
```

#### Runs

A run is one named change and the repos it applies to. `RUN` is its name, chosen by you, e.g. `bump-ci`. The name:

- names the run's directory, `runs/RUN/` next to the database (default `~/.local/share/repodb/runs/RUN/`; `--workdir DIR` overrides it).
- names the branch, `repodb/RUN`, unless `--branch` is given when the run is created. The branch is fixed afterwards.
- identifies the run to `review`, `publish` and `runs`.

A name is letters, digits, `.`, `_` and `-`, starting with a letter or digit, and `repodb/RUN` must be a valid git branch.

The run directory holds `run.json` (the change and each repo's state), a shallow clone per repo under `repos/OWNER/NAME/`, a log per repo under `logs/`, and the script for `--script`. Your own working copies are never touched.

#### Select repos

A new run needs a selection. There is no implicit "all".

```sh
-r SPEC [-r ...]        # named projects: OWNER/NAME, or NAME if unambiguous
-s SET                  # a set
-t TOPIC [-t ...] [--any]
--owner USER
--all                   # every project in the database
```

`-s`, `-t` and `--owner` combine as in [Select projects](#select-projects); `-r` goes alone. A selection given to an existing run adds those repos to it. A repo stored under two names is applied once.

#### The change

Each run has exactly one change: `--script`, `--exec`, or `--replace`. All three need `-m MESSAGE` when the run is created. The message's first line is the commit subject and PR title; the rest is the PR body.

**`--script FILE`** runs a script in each repo. Use it for anything beyond a one-line command.

```sh
repodb template sh > fix.sh       # or: repodb template py > fix.py
$EDITOR fix.sh
repodb apply RUN -s SET --script fix.sh -m "Message"
```

- FILE needs a `#!` line; it runs through it. Python scripts therefore use `python3` from your `PATH`, not repodb's interpreter.
- FILE is copied into the run as `runs/RUN/script`, and its sha256 is recorded. Each repo runs that copy.
- Editing FILE and rerunning with `--script FILE` is a changed change: it needs `--redo`. Rerunning without `--script` uses the stored copy.
- Only FILE is copied. Modules or data files beside it are not.
- Both templates fail until edited, so an unedited template commits nothing. The Python template includes `substitute(path, pattern, repl)`, a regex substitution that keeps line endings.

**`--exec CMD`** runs a shell command (`/bin/sh -c CMD`) in each repo. Use it for one-liners.

```sh
repodb apply fmt -s SET --exec 'npx prettier --write .' -m "Format with prettier"
repodb apply node20 -t node --exec 'npm pkg set engines.node=">=20"' -m "Require Node 20"
```

- The working directory is the clone, so a relative path in CMD resolves inside each repo. For a script of your own, use `--script`.
- A changed CMD needs `--redo`.

**`--replace PATTERN REPL --glob GLOB`** substitutes a Python regex in files, without running any program.

```sh
repodb apply checkout-v5 --all --replace 'actions/checkout@v\d+' 'actions/checkout@v5' \
  --glob '.github/workflows/*.yml' --glob '.github/workflows/*.yaml' -m "Bump actions/checkout to v5"
```

- PATTERN is Python `re` syntax, applied to each whole file. REPL may use `\1` or `\g<name>`.
- GLOB is relative to the repo root; `**` matches any depth. `--glob` repeats. An absolute GLOB or one containing `..` is refused.
- Only files tracked by git, UTF-8, and not symlinks are changed. Line endings are kept.
- `review` shows the match count per repo, so a pattern that matched nowhere is visible.

**Contract for `--script` and `--exec`:**

| The command... | Result |
|-|-|
| exits 0 and changed files | `committed`: repodb runs `git add -A` and commits |
| exits 0 and changed nothing | `unchanged`: nothing to publish |
| exits non-zero, or exceeds `--timeout SECS` | `failed`, with the error recorded |

- The working directory is the root of a fresh `--depth 1` clone of the default branch. History is not available.
- The environment adds `REPODB_RUN`, `REPODB_OWNER`, `REPODB_NAME` and `REPODB_URL`.
- Output goes to `runs/RUN/logs/OWNER__NAME.log`.
- Do not commit, push or switch branches. repodb does that.
- Leave no temporary files: `git add -A` stages every new file. `review` lists the files of each commit, so strays are visible before `publish`.
- `--timeout` kills the command and its child processes.

#### Other `apply` options

```sh
-j N               # repos at once (default 1)
--timeout SECS     # per-repo limit for --exec or --script
--branch NAME      # branch for a new run (default repodb/RUN)
--workdir DIR      # directory holding runs
--redo [SPEC ...]  # start these repos over (all if none given)
```

`apply` prints one line per repo as it finishes, then a summary. It exits 1 if any repo failed.

#### States and resuming

| State | Meaning | Next |
|-|-|-|
| `selected` | added, not yet tried | `apply` |
| `committed` | changed and committed locally | `publish` |
| `unchanged` | the change made no difference | nothing |
| `failed` | clone, command or commit failed | rerun `apply` |
| `published` | pushed; PR opened unless `--push-default` | nothing |

- Rerunning `apply RUN` retries `selected` and `failed` repos and skips the rest. An interrupted run (Ctrl-C, crash) keeps finished repos.
- A different `--script` file, `--exec`, `--replace`, `--glob` or `-m` is refused unless `--redo` is given. `--redo` records the new change and resets the named repos, or all repos, to `selected`.
- `--redo` refuses published repos. Once any repo is published, name the repos to redo.

```sh
repodb apply bump-ci --script fix.py --redo               # all repos, with the edited script
repodb apply bump-ci --redo alice/api web                 # only these repos, same change
repodb apply bump-ci -r newrepo                           # add a repo to the run
```

#### Review

```sh
repodb review RUN                 # one line per repo, grouped by state
repodb review RUN SPEC ... --diff # also the commit's stat and patch
```

A line reads `committed  alice/api  1 file changed, 1 insertion(+), 1 deletion(-); 2 matches`. Failed repos show their error. The repo's log is in `runs/RUN/logs/`.

#### Publish

```sh
repodb publish RUN [SPEC ...] [--draft]   # PR mode: push repodb/RUN, open a PR per repo
repodb publish RUN --push-default         # push the commit to the default branch, no PR
```

- Only `committed` repos are published. `publish` can be rerun; it skips `published` repos.
- PR mode needs [`gh`](https://cli.github.com) and a github.com url. Other hosts need `--push-default`.
- Before pushing, `publish` fetches the default branch. If it moved since `apply`, the repo is refused with `redo SPEC`: rebasing could hide a conflict.
- A failed push or PR keeps the repo `committed` with the error, so a rerun retries it. If a PR for the branch already exists, it is recorded.
- Changes under `.github/workflows/` pushed over HTTPS need the token's `workflow` scope. `publish` checks first and stops with `gh auth refresh -s workflow`.
- `-j` defaults to 1, since GitHub rate-limits PR creation.
- `publish` exits 1 if any repo failed.

#### Manage runs

```sh
repodb runs                       # each run and its state counts
repodb runs RUN --discard         # delete the run's directory: clones, logs, script
```

#### Notes

- Commits use your git identity and signing config. With `commit.gpgsign` and an uncached key, each repo prompts. Override per run with `GIT_AUTHOR_*`/`GIT_COMMITTER_*`, or `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=commit.gpgsign GIT_CONFIG_VALUE_0=false`.
- HTTPS pushes need a credential helper, e.g. `gh auth setup-git`. SSH urls need a loaded key.
- `apply`, `publish` and `runs --discard` lock the run. A second process on the same run exits with a message.
- Archived repos reject pushes; repodb does not know which repos are archived or forks.
- Design and failure modes: [`docs/dev/apply.md`](docs/dev/apply.md).

### JSON

The JSON form is `{name: url}`, or with `-g` `{owner: {name: url}}`. A project with topics has `{"url": url, "topics": [...]}` in place of the url. Both forms round-trip byte for byte. Sets are not included.

The flat forms refuse a name held by two owners, ignoring case; use `-g`.

The database defaults to `~/.local/share/repodb/repos.sqlite`; override with `--db`. Runs are kept in `runs/` next to it.

## Library

```python
from pathlib import Path
from repodb import GitRepoDB, Run, apply_run, clone, runs_dir
from repodb.apply import check_script, install_script, locked

with GitRepoDB() as db:
    rows = db.rows(set_name="agents")
    failed = clone(rows, Path("~/src").expanduser(), jobs=4)

    run = Run.create("bump", "Bump checkout", replace=[r"checkout@v\d+", "checkout@v5"],
                     glob=[".github/workflows/*.yml"])
    run.add(rows)
    root = runs_dir(db.db_path) / run.name
    with locked(root):                     # keeps other processes out of the run
        apply_run(run, root, jobs=8)       # then inspect run.repos; publish_run(run, root)
```

A script run records the script's digest and installs the same bytes:

```python
data = Path("fix.py").read_bytes()
run = Run.create("fix", "Fix things", script=check_script(data))
install_script(root, data)                 # before apply_run
```

Functions return data and raise exceptions. Progress goes to the `repodb` logger, which is silent unless configured. See the docstrings in `repodb.core` and `repodb.apply`.

## Development

```sh
make qa     # ruff lint and format check, mypy, pytest
```
