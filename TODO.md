# TODO

## Critical

## High

## Medium

- [ ] Run one manual `publish` in PR mode against a scratch GitHub repo. PR mode is tested only with `gh` mocked.

- [ ] `status` reports git state per clone: `dirty`, `ahead N`, `behind N`, `detached`, from one `git status --porcelain=v2 --branch` call. Today it checks only presence, `.git`, and origin. Read-only half of the next item, so it can ship first.

- [ ] `clone --update` fast-forwards clones that exist instead of skipping them: verify origin matches the stored url, `git fetch --prune`, refuse a dirty worktree, `pull --ff-only` only when behind and not ahead. Any other state (ahead, diverged, dirty, detached) is reported and left alone, matching `publish`'s refusal to rebase. No `reset` or `rebase` strategy; repodb has no destructive operations. Alternative: `repodb list` piped to `xargs git pull --ff-only`, which loses the per-repo report.

- [ ] `refresh --prune` confirms each unlisted repo with `gh api repos/OWNER/NAME` before marking it prunable. Only a 404 is prunable; 403, network errors and other failures are reported as unclear and never pruned. A 301 means renamed or transferred: report `renamed to FULL_NAME` instead. The listing-completeness guard (`partial`) stays as the first filter, so the normal path makes no extra calls. Closes a hole: a renamed repo is unlisted under its old name and is marked prunable today. A 404 does not prove deletion: GitHub returns 404 for a private repo the token cannot see. Fetch `isPrivate` in `gh repo list` and store it; never auto-prune a private repo, report it as unclear.

## Low

- [ ] L7. Url parsing gaps (`core.py:43`, `core.py:592`): `OWNER` needs a dot in the host, so `git@gitserver:team/repo` is not storable; `ssh.github.com` is not treated as GitHub; `--slugs` prints `group/repo` for GitLab `group/sub/repo`.

- [ ] Docs: `docs/dev/apply.md` is stale (line 38 `review` syntax, line 162 says `failed` where code keeps `committed`, line 182 estimate). Split into a user guide and a decision record.

- [ ] Docs: no API reference. The README is the reference today.

- [ ] Tests: split `tests/test_repodb.py` (1800 lines) into `test_core.py` and `test_cli.py`.

- [ ] Tests: cover `--help` output (`metavar="COMMAND"`).

- [ ] Tests: cover error paths: `gh pr create` failure, `KeyboardInterrupt` in `parallel`, `cmd_runs` without `--discard`, invalid `--replace` replacement.

- [ ] PR tracking for published runs: `runs RUN --prs` via `gh pr view --json state`.

- [ ] `tag OWNER/NAME TOPIC` for projects on hosts without topic fetching (GitLab, Codeberg). `--refresh` would overwrite local tags on GitHub repos, so this needs a `source` column on `topics`.

- [ ] More GitHub metadata (language, archived, fork, last push) for filters such as `clone --language rust`. Archived and fork flags would let `apply` skip repos that reject pushes or should not change.

- [ ] Sets in JSON export/import, so a set defined on one machine reaches another. A top-level `sets` key would clash with an owner named `sets` in the grouped form.

- [ ] Rewrite stored urls between HTTPS and SSH without re-fetching.
