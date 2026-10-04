# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Runs have a lock and a format version. `apply`, `publish` and `runs --discard` hold `runs/.RUN.lock`; a second process on the same run exits with a message. Before, two processes each saved `run.json` from their own copy, so one silently dropped the other's results. `run.json` gains `"version": 1`. A newer version is refused, and unknown keys are ignored, so an added field stays readable by older releases.

### Fixed

- `apply --all` or `--owner` that selects nothing now says so. Before, it exited 1 with no message.

- Tests no longer read the user's git config. `commit.gpgsign = true` or a `url.insteadOf` rewrite made them fail.

- `apply` and `publish`: one repo's unexpected error, such as an `OSError` or a missing `gh`, no longer aborts the run. Before, it stopped result collection, so repos already finished, possibly already pushed, were not saved to `run.json`, and queued work kept running. Now the error goes into that repo's state as `failed` (apply) or `committed` with the error (publish), and the traceback is logged.

- `publish`: a retry no longer stays `committed` when an earlier publish opened the PR but did not record it, for example after a lost `gh` response. `gh pr create` then fails because the PR exists. On that failure, `publish` now records the open PR for the run's branch. The lookup runs only after a failure, so the normal path makes no extra `gh` call.

## [0.2.0]

### Added

- Topics. `github` stores each repo's GitHub topics in a new `topics` table, replacing them on each run. `-t TOPIC` selects by topic in `list`, `export`, `clone` and `status`; repeated, a project needs every topic, or with `--any` at least one. A `-t` that matches nothing exits 1, so a mistyped topic is not a silent success. `topics` prints each topic with its project count, `list --topics` shows each project's topics, and `info` reports the most used. A separate table rather than a column lets existing databases open without migration.

- `github --refresh [USER]` updates the topics of stored github.com projects, including ones added by `scan`, and adds none. It matches by the repo name in the URL, since `scan` stores the directory name. `--prune` lists stored repos `gh` no longer lists, and `--yes` removes them. An owner whose listing is empty or reaches `--limit` is not pruned: an empty listing can mean lost access, and a full one can be truncated.

- JSON files carry topics: a value may be `{"url": url, "topics": [...]}` in place of the url. `export` writes that form only for projects with topics, so files without topics are unchanged. An object is read as a project only if its keys are exactly `url` and `topics` with a list value; a group's values are never lists, so this cannot match a group. On `import`, a bare url leaves stored topics alone and `"topics": []` clears them. Files with topics are not readable by 0.1.2.

- Sets. `set --name SET SPEC ...` adds projects to a named set, stored in a new `sets` table; `-s SET` selects it wherever `-t` works. Sets are local and independent of topics, for selections topics cannot express. Adding is idempotent and all-or-nothing; `--remove` and `--delete` change or drop a set. The selector is `-s` because `-g/--group` already means "group by owner". Sets are not carried by JSON export.

- `clone -r SPEC` clones named projects, given as `OWNER/NAME` or an unambiguous `NAME`.

- `clone -j N` runs N clones at once. Each clone's output is captured and printed when it finishes, since interleaved progress from N clones is unreadable.

- `clone DEST -- OPTION ...` passes options to `git clone`, e.g. `-- --depth 1`. Words after DEST without `--` are rejected, so a typo cannot reach git as an option.

- `status DEST` compares a clone directory with the database: missing clones, untracked directories, and clones whose `origin` differs from the stored url. It exits 1 on any difference, so scripts can test it. Untracked is judged against the whole database, so filters do not report other projects' clones.

- `list --slugs` prints `OWNER/REPO` from each url, once per repo, for multi-repo tools such as git-xargs or multi-gitter. It reads the url, not the stored name, since `scan` stores the directory name.

- `apply`, `review`, `publish` and `runs` change many repos in two steps. `apply RUN` clones each selected repo into a work directory, runs `--exec CMD` or a built-in `--replace PATTERN REPL --glob GLOB`, and commits on `repodb/RUN`. `publish RUN` pushes and opens a PR per repo via `gh`, or with `--push-default` pushes to the default branch. Preparing and publishing are separate so changes to many repos can be reviewed before anything is pushed. Progress is kept in `run.json`, so rerunning either command resumes. `publish` refuses a repo whose default branch moved since `apply`, rather than rebasing, and checks the `workflow` token scope before pushing workflow changes over HTTPS. Design: `docs/dev/apply.md`.

### Changed

- repodb is usable as a library. The CLI moved to `repodb.cli`, and the console script to `repodb.cli:main`. Library functions no longer print, read `argparse` namespaces, or exit: `GitRepoDB.add` returns `Change` records, `info` returns `(field, value)` pairs, `status` returns `Difference` records, and `refresh` returns a `Refresh` report and removes nothing. `scan` and `clone` log skips and progress to the `repodb` logger, which is silent unless configured; the CLI prints them for the length of one command. Runs have a Python API: `Run.create`, `run.add`, `run.redo`, `apply_run` and `publish_run`. The package exports these names. Library errors no longer name CLI flags; the CLI adds hints such as "use --group".

- `repodb.core.read_pairs` is now `read_projects`, and returns each project's topics too.

- Rows are keyed by url, not `(owner, name)`. One repo could be stored under two names, e.g. its directory name from `scan` and its repo name from `github`, and `clone` and `apply` then changed it twice. Urls now match by host and path, ignoring scheme, user, port, `.git` and case; adding a stored url updates it and keeps its name. `(owner, name)` stays unique because it names the clone directory, so a new url whose `(owner, name)` is taken is rejected; before, it overwrote the other url. `clone --json` and `Run.add` skip a repeated url the same way.

  A 0.1.x database is migrated when first opened, in one transaction, after a copy to `<db>.0.1.bak`. Rows with the same url merge into the first stored, keeping its name; their topics and sets move to it, and each merge is logged. Migration over refusal because the only other route, export with 0.1.x then import, needs the old version installed. The migration refuses to run if the backup path exists, rather than overwrite an earlier backup.

### Fixed

- `scan` stored the origin url after `url.<base>.insteadOf` rewriting, so a local rule such as `insteadOf https://github.com/` stored an SSH url that other machines might not reach. It now stores the url as configured.

- A url whose owner is `.` or `..`, such as `https://github.com/../r`, was stored with that owner. `clone -g` refused it, but `apply` joined it into a path and could delete a directory outside the run. Such urls now have no owner. `GitRepoDB.add` rejects a name that is not a single path component; before, only `import` checked. `clone` clones nothing if any target is invalid, or with `-g` has no owner; before, it skipped those rows, so a mistyped selection partly ran.

## [0.1.2]

### Fixed

- Importing `repodb` failed with `ModuleNotFoundError: typing_extensions` outside the dev environment. The import is only needed for type checking. mypy pulls it into the dev venv, so tests passed, and `test_has_no_runtime_dependencies` checks declared metadata, not imports. `test_imports_only_stdlib` now imports the package with non-stdlib modules blocked.

## [0.1.1]

### Removed

- The `listrepos` console script. It discarded its arguments, so `listrepos --owner X` listed every owner. Use `repodb list`, or a shell alias.

## [0.1.0]

### Added

- Moved from `misc-utils/src/py/repodb.py` and packaged, with `repodb` and `listrepos` console scripts. Rewritten on SQLite, with `gitprojects.py` folded in. Subcommands: `scan DIR` and `github USER` (via `gh repo list`) add projects; `export`/`import` move `{name: url}` JSON; `clone DEST` clones each into `DEST/<name>`, skipping names that exist; `remove OWNER/NAME|NAME ...` deletes rows, all or none, refusing a bare name two owners share, and `remove --owner USER --all` deletes all of one owner's rows, `--all` being required so one flag cannot delete many; cloned directories are kept. `info` reports format, counts, hosts, top owners and names shared across owners, opening the file read-only. `list` replaces the old flags. Rows are keyed by `(owner, name)`, owner being the first path component after the host in the URL, for any host. `--owner` filters `export`, `list` and `clone`, and `-g` groups them: `{owner: {name: url}}` JSON, an indented listing, or `DEST/<owner>/<name>`. `import` and `clone --json` read either JSON form, taking owner from the URL rather than the group key.

  Owner is required (`NOT NULL`, non-empty), so local-path and `file://` remotes are skipped by `scan` and rejected by `import`. Keying on name alone let `bob/r` silently replace `alice/r`; the flat forms now reject a name held by two owners, compared case-insensitively since `Foo` and `foo` are one directory on macOS, and point to `-g`. The old `dbm` store held URLs as `Path`, which collapses `https://` to `https:/`. A table with any other primary key is refused rather than migrated. Names and owners that are not a single path component are rejected, since they would clone outside `DEST`. The default database is `~/.local/share/repodb/repos.sqlite`.
