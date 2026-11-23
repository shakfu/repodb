# Decision record: `apply` and `publish`

Usage is in the README, under "Change many repos". This file records what was chosen, over what, and why. Code: `src/repodb/apply.py`; CLI: `src/repodb/cli.py`.

## Scope

- **`apply` stays in repodb**, over a separate tool fed by `repodb list --slugs` (decided 2026-10-04). It reuses repodb's selection (`-s`, `-t`, `--owner`, `-r`). It holds most of the project's risk, so it gets the same tests and review.
- External tools remain an option: [git-xargs](https://github.com/gruntwork-io/git-xargs), [multi-gitter](https://github.com/lindell/multi-gitter), [turbolift](https://github.com/Skyscanner/turbolift). They take `list --slugs` output.
- For action pins, [Dependabot's `github-actions` ecosystem](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/keeping-your-actions-up-to-date-with-dependabot) keeps them current per repo. A sweep fixes versions once, and they drift again. Rolling out `dependabot.yml` is itself a sweep.

## Two steps

`apply` commits locally; `publish` pushes. This was chosen over one command because pushing 200 unreviewed changes is the main risk. `review` sits between them.

## Run state

- **`run.json` in the run directory**, over tables in `repos.sqlite`. A run is temporary state for one change. A file can be inspected and goes away with `runs --discard`. The database stays a list of projects.
- The main thread saves after each repo, through a temporary file and `os.replace`. A crash or Ctrl-C keeps finished repos.
- `version` is raised only when older code would misread the file. Unknown keys are ignored, so added fields (e.g. `pushed`) need no bump.

## Lock

- A non-blocking `flock` on `runs/.RUN.lock`, held from loading `run.json` to its last save. A second `apply`, `publish` or `runs --discard` on the run exits.
- The OS releases it when the process dies, so there is no stale lock.
- It sits beside the run directory, so a new `apply` that fails creates no directory, and `--discard` deletes the directory while holding the lock.
- It is never deleted. Unlinking a lock file another process has open lets two processes hold it.

## Per-repo work

- **A fresh `--depth 1` clone per repo**, over the user's working copies. Those can be dirty, on another branch, or behind origin.
- **`git add -A`**, over tracked files only. Changes such as adding `dependabot.yml` create files. Stray output is also staged, so `review` lists each commit's files.
- **States:** `selected`, `committed`, `unchanged`, `failed`, `published`. A failed `apply` marks the repo `failed`, so `apply` retries it. A failed `publish` keeps it `committed` with the error, so `publish` retries it.
- **A new run needs an explicit selection.** A missing filter cannot select every project.

## The change

- **`--replace` is built in**, over `--exec 'perl -pi ...'`. It avoids shell and perl quoting: in `s/checkout@v3/.../`, perl interpolates `@v3` as an array. It also counts matches per file, so a pattern that matched nowhere is visible in `review`. It writes only tracked, UTF-8, non-symlink files. A symlink's target can be outside the repo.
- **The replacement is validated before cloning.** `re` parses a replacement only on a match, so a bad `\2` used to fail in every repo after every clone.
- **`--script` copies the file into the run and records its sha256**, over running it by path. `--exec` runs in each clone, so a relative path does not resolve. An edit between runs would reach only the repos resumed after it. The digest, over the mtime, makes an edited script a changed change that needs `--redo`.
- **Commands run in their own session**, so `--timeout` kills their children. The terminal's Ctrl-C then does not reach them, so `parallel` kills them on interrupt.

## Publish

- **A moved default branch is refused**, over rebasing. A rebase could hide a conflict or a semantic clash. The user redoes the repo.
- **PR mode pushes `repodb/RUN`.** The first push is a plain push. Later pushes use `--force-with-lease` on the commit repodb last pushed, recorded as `pushed`. A redone commit does not descend from the pushed one, so a plain push is rejected. `--force` would overwrite commits others added to the PR branch.
- **A failed `gh pr create` looks up an open PR** for the same repo, base and head. A retry after a lost response is then recorded, not stuck.
- **The `workflow` scope is checked once, before any push**, when a commit touches `.github/workflows/` over HTTPS. GitHub rejects such pushes without it. The check needs `gh auth status --json`; without it, `publish` stops rather than parse text that lists other accounts.
- **`publish -j` defaults to 1.** GitHub rate-limits content creation such as PRs.
- **`--push-default`** pushes `repodb/RUN:DEFAULT`. It works on any host. Branch protection rejects it per repo, and the error is recorded.

## Known limits

- Archived repos reject pushes, and forks usually should not change. repodb stores neither flag (see TODO).
- HTTPS pushes need a credential helper, e.g. `gh auth setup-git`. SSH urls need a loaded key.
- A push from a shallow clone needs the server to have the base commit. GitHub does, and a moved branch is refused first.
- PR tracking (`runs RUN --prs`) is deferred (see TODO).
- `scripts/smoke-github.sh` exercises PR mode against real GitHub. The test suite mocks `gh`.
