# repodb

Keep a SQLite database of git project clone URLs. Clone, compare and change the projects in bulk.

Each project is stored as `(owner, name, url)`, keyed by url: https and ssh urls of one repo are one project. The name is the clone directory, and `OWNER/NAME` is unique too. The owner is the first path component after the host (`alice` in `github.com/alice/r` or `git@gitlab.com:alice/r`), for any host.

## Install

```sh
pip install repodb
```

This installs `repodb`.

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

```sh
repodb apply RUN -s SET --exec 'python fix.py' -m "Message"
repodb apply RUN -s SET --replace 'actions/checkout@v\d+' 'actions/checkout@v5' \
  --glob '.github/workflows/*.y*ml' -m "Bump actions/checkout to v5" -j 8
repodb review RUN [SPEC ...] [--diff]     # per-repo state; full diffs
repodb publish RUN [--draft]              # push repodb/RUN, open a PR per repo (needs gh)
repodb publish RUN --push-default         # push to the default branch instead
repodb runs [RUN --discard]               # list runs, or delete one
```

- `apply` clones each repo into a work directory, runs the change, and commits on `repodb/RUN`. Nothing is pushed until `publish`.

- Rerunning `apply` or `publish` resumes the run. A changed command or message needs `--redo`.

- `publish` refuses a repo whose default branch moved since `apply`.

- Edits under `.github/workflows/` pushed over HTTPS need `gh auth refresh -s workflow`.

- Details: [`docs/dev/apply.md`](docs/dev/apply.md).

### JSON

The JSON form is `{name: url}`, or with `-g` `{owner: {name: url}}`. A project with topics has `{"url": url, "topics": [...]}` in place of the url. Both forms round-trip byte for byte. Sets are not included.

The flat forms refuse a name held by two owners, ignoring case; use `-g`.

The database defaults to `~/.local/share/repodb/repos.sqlite`; override with `--db`. Runs are kept in `runs/` next to it.

## Library

```python
from pathlib import Path
from repodb import GitRepoDB, Run, apply_run, clone, runs_dir

with GitRepoDB() as db:
    rows = db.rows(set_name="agents")
    failed = clone(rows, Path("~/src").expanduser(), jobs=4)

    run = Run.create("bump", "Bump checkout", replace=[r"checkout@v\d+", "checkout@v5"],
                     glob=[".github/workflows/*.yml"])
    run.add(rows)
    apply_run(run, runs_dir(db.db_path) / run.name)
```

Functions return data and raise exceptions. Progress goes to the `repodb` logger, which is silent unless configured. See the docstrings in `repodb.core` and `repodb.apply`.

## Development

```sh
make qa     # ruff lint and format check, mypy, pytest
```
