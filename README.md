# repodb

Keep a SQLite database of git project clone URLs, and clone from it.

Each project is stored as `(owner, name, url)`, keyed by `(owner, name)`. The owner is the first path component after the host (`alice` in `github.com/alice/r` or `git@gitlab.com:alice/r`), for any host.

## Install

```bash
uv tool install git+https://github.com/shakfu/repodb
```

This installs `repodb`, and `listrepos` as an alias for `repodb list`.

## Usage

```bash
repodb scan ~/src                         # add origin URLs of local projects
repodb github USER [--source] [--ssh]     # add USER's GitHub repos (needs gh)
repodb list [-g] [-u] [--owner USER]      # names, grouped by owner, or URLs
repodb export [-g] [--owner USER] -o projects.json
repodb import projects.json
repodb clone DEST [-g] [--owner USER | --json projects.json]
repodb remove OWNER/NAME [NAME ...] | --owner USER --all
repodb info                               # format, counts, hosts; read-only
```

- The JSON form is `{name: url}`, or with `-g` `{owner: {name: url}}`. Both round-trip byte for byte.

- `clone` writes `DEST/<name>`, or with `-g` `DEST/<owner>/<name>`, and skips targets that exist.

- The flat forms refuse a name held by two owners, ignoring case; use `-g`.

- The database defaults to `~/.local/share/repodb/repos.sqlite`; override with `--db`.

## Development

```bash
make qa     # ruff lint and format check, mypy, pytest
```
