# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0]

### Added

- Moved from `misc-utils/src/py/repodb.py` and packaged, with `repodb` and `listrepos` console scripts. Rewritten on SQLite, with `gitprojects.py` folded in. Subcommands: `scan DIR` and `github USER` (via `gh repo list`) add projects; `export`/`import` move `{name: url}` JSON; `clone DEST` clones each into `DEST/<name>`, skipping names that exist; `remove OWNER/NAME|NAME ...` deletes rows, all or none, refusing a bare name two owners share, and `remove --owner USER --all` deletes all of one owner's rows, `--all` being required so one flag cannot delete many; cloned directories are kept. `info` reports format, counts, hosts, top owners and names shared across owners, opening the file read-only. `list` replaces the old flags. Rows are keyed by `(owner, name)`, owner being the first path component after the host in the URL, for any host. `--owner` filters `export`, `list` and `clone`, and `-g` groups them: `{owner: {name: url}}` JSON, an indented listing, or `DEST/<owner>/<name>`. `import` and `clone --json` read either JSON form, taking owner from the URL rather than the group key.

  Owner is required (`NOT NULL`, non-empty), so local-path and `file://` remotes are skipped by `scan` and rejected by `import`. Keying on name alone let `bob/r` silently replace `alice/r`; the flat forms now reject a name held by two owners, compared case-insensitively since `Foo` and `foo` are one directory on macOS, and point to `-g`. The old `dbm` store held URLs as `Path`, which collapses `https://` to `https:/`. A table with any other primary key is refused rather than migrated. Names and owners that are not a single path component are rejected, since they would clone outside `DEST`. The default database is `~/.local/share/repodb/repos.sqlite`.
