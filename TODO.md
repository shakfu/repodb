# TODO

## Critical

## High

- [x] H1. CI type check fails. CI runs `mypy src/repodb tests/`; `make typecheck` runs `mypy src/repodb`. Make the target match CI, then fix the 3 `[index]` errors in `tests/test_repodb.py:771`, `:784` and `tests/test_library.py:82` (`dict[str, object]` values indexed).

- [x] H2. `apply.prepare` can `rmtree` a directory outside the run directory. Owner and name are not validated (`apply.py:305-310`). Validate both with `valid_name` in `Run.add`, and reject invalid names in `GitRepoDB.add`.

- [x] H3. One repo stored under two names (`scan` dir name vs `github` repo name) is cloned and changed twice. De-duplicate by normalised url in `Run.add` and `clone`. Root cause is M6.

## Medium

- [ ] M1. A non-`Failed` exception in one repo aborts `apply.parallel` and loses finished, uncollected results. Catch `Exception` per repo in the worker and record it as `failed`.

- [ ] M2. Tests depend on global git config: `commit.gpgsign = true` gives 52 errors; `url.insteadOf` gives 2 failures. Set `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` to `/dev/null` in an autouse fixture in `tests/conftest.py`. Document the `commit.gpgsign` prompt per parallel commit in `docs/dev/apply.md`.

- [ ] M3. `apply` exits 1 with no message on an empty selection via `--all` or `--owner` (`cli.py:484-485`).

- [ ] M4. Ctrl-C does not stop `clone -j N`: `with ThreadPoolExecutor` waits for all submitted tasks (`core.py:766`). Share `apply.parallel`'s `cancel_futures=True` handling.

- [ ] M5. Runs have no lock and no format version. Add a lock file in the run directory, a `version` field in `run.json`, and ignore unknown keys in `Run.load`.

- [x] M6. Decide project identity before adding features. Key is `(owner, name)`; identity is the url. Alternative: key on normalised `host/owner/repo`, with the directory name as an attribute. Schema change; current schema refuses migration.

- [ ] Decide scope: keep `apply` in repodb, or move it to its own package consuming `repodb list --slugs`. It holds most of the risk (H2, H3, M1, M5, L3, L5, L6).

- [ ] Run one manual `publish` in PR mode against a scratch GitHub repo. PR mode is tested only with `gh` mocked.

## Low

- [ ] L1. `export -o` to an unwritable path prints a traceback (`cli.py:205`).

- [ ] L2. A read command with a mistyped `--db` creates an empty database and its parent directory. `info` does not.

- [ ] L3. Validate `--branch` up front with `git check-ref-format --branch`. An invalid name now fails in every repo after every clone; a leading `-` is read by git as an option.

- [ ] L4. `github -L` accepts 0 and negative values. Use the existing `positive` type.

- [ ] L5. `has_workflow_scope` substring-matches `workflow` in `gh auth status`; with several accounts or hosts it can match the wrong one.

- [ ] L6. PR mode cannot recover when the PR already exists: `gh pr create` fails on each retry and the repo stays `committed`.

- [ ] L7. Url parsing gaps (`core.py:43`, `core.py:592`): `OWNER` needs a dot in the host, so `git@gitserver:team/repo` is not storable; `ssh.github.com` is not treated as GitHub; `--slugs` prints `group/repo` for GitLab `group/sub/repo`.

- [ ] L8. `apply --exec` uses POSIX-only `os.killpg` and `start_new_session`. Declare the OS in `pyproject.toml` or support Windows.

- [ ] L9. `__version__` is duplicated in `__init__.py` and `pyproject.toml`. Use `importlib.metadata.version("repodb")`.

- [ ] Docs: `docs/dev/apply.md` is stale (line 38 `review` syntax, line 162 says `failed` where code keeps `committed`, line 182 estimate). Split into a user guide and a decision record.

- [ ] Docs: date `CHANGELOG.md` `[0.2.0]` as `## [0.2.0] - YYYY-MM-DD`.

- [ ] Docs: state requirements in `README.md`: `git`, and `gh` for `github` and PR mode.

- [ ] Docs: no API reference; `make docs` has no `docs/conf.py`.

- [ ] Tests: split `tests/test_repodb.py` (1579 lines) into `test_core.py` and `test_cli.py`.

- [ ] Tests: cover `--help` output (`metavar="COMMAND"`).

- [ ] Tests: cover error paths: `gh pr create` failure, `KeyboardInterrupt` in `parallel`, `cmd_runs` without `--discard`, invalid `--replace` replacement.

- [ ] CI: the `qa` job, including the type check, runs on Python 3.13 only.

- [ ] CI: verify action versions exist (`actions/checkout@v7`, `astral-sh/setup-uv@v10.0.1`, `upload-artifact@v7`, `download-artifact@v8`).

- [ ] CI: `collect-artifacts` merges two identical pure-Python wheels.

- [ ] Rerun the suite on 3.10, 3.12 and 3.14 after the library refactor.

- [ ] PR tracking for published runs: `runs RUN --prs` via `gh pr view --json state`.

- [ ] `tag OWNER/NAME TOPIC` for projects on hosts without topic fetching (GitLab, Codeberg). `--refresh` would overwrite local tags on GitHub repos, so this needs a `source` column on `topics`.

- [ ] More GitHub metadata (language, archived, fork, last push) for filters such as `clone --language rust`. Archived and fork flags would let `apply` skip repos that reject pushes or should not change.

- [ ] Sets in JSON export/import, so a set defined on one machine reaches another. A top-level `sets` key would clash with an owner named `sets` in the grouped form.

- [ ] Rewrite stored urls between HTTPS and SSH without re-fetching.
