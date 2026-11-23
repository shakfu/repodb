# TODO

## Critical

## High

## Medium

- [ ] `status` reports git state per clone: `dirty`, `ahead N`, `behind N`, `detached`, from one `git status --porcelain=v2 --branch` call. Today it checks only presence, `.git`, and origin. Read-only half of the next item, so it can ship first.

- [ ] `clone --update` fast-forwards clones that exist instead of skipping them: verify origin matches the stored url, `git fetch --prune`, refuse a dirty worktree, `pull --ff-only` only when behind and not ahead. Any other state (ahead, diverged, dirty, detached) is reported and left alone, matching `publish`'s refusal to rebase. No `reset` or `rebase` strategy; repodb has no destructive operations. Alternative: `repodb list` piped to `xargs git pull --ff-only`, which loses the per-repo report.

## Low

- [ ] L7. Url parsing gaps (`core.py:43`, `core.py:592`): `OWNER` needs a dot in the host, so `git@gitserver:team/repo` is not storable; `ssh.github.com` is not treated as GitHub; `--slugs` prints `group/repo` for GitLab `group/sub/repo`.

- [ ] Docs: no API reference. The README is the reference today.

- [ ] Tests: cover `--help` output (`metavar="COMMAND"`).

- [ ] Tests: cover `runs RUN` without `--discard`.

- [ ] PR tracking for published runs: `runs RUN --prs` via `gh pr view --json state`.

- [ ] `tag OWNER/NAME TOPIC` for projects on hosts without topic fetching (GitLab, Codeberg). `--refresh` would overwrite local tags on GitHub repos, so this needs a `source` column on `topics`.

- [ ] More GitHub metadata (language, archived, fork, last push) for filters such as `clone --language rust`. Archived and fork flags would let `apply` skip repos that reject pushes or should not change.

- [ ] Sets in JSON export/import, so a set defined on one machine reaches another. A top-level `sets` key would clash with an owner named `sets` in the grouped form.

- [ ] Rewrite stored urls between HTTPS and SSH without re-fetching.
