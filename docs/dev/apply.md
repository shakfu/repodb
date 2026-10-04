# Design: `apply` and `publish`

Status: implemented in `src/repodb/apply.py`, except PR tracking. `repodb list --slugs` with an external tool remains an alternative.

## Use cases

1. Bump pinned actions (e.g. `actions/checkout@v3` to `@v4`) in `.github/workflows/` across ~200 repos.

2. Run a script that edits a repo, then commit and push the result, across a selection of repos.

## External tools

Feed `repodb list --slugs [-s SET] [-t TOPIC ...] [--owner U]` to one of:

- [git-xargs](https://github.com/gruntwork-io/git-xargs): `--repos FILE` of `owner/name` lines.

- [multi-gitter](https://github.com/lindell/multi-gitter): `--repo` per slug, or its own `--org`/`--topic` selection; `--skip-pr` pushes directly.

- [turbolift](https://github.com/Skyscanner/turbolift).

For use case 1, [Dependabot's `github-actions` ecosystem](https://docs.github.com/en/code-security/dependabot/working-with-dependabot/keeping-your-actions-up-to-date-with-dependabot) keeps actions current per repo. A one-off sweep fixes versions once, and they drift again. Rolling out `dependabot.yml` is itself a one-off multi-repo change.

## Decisions

- Two steps. `apply` prepares commits in a work directory and prints a diff summary; `publish` pushes after review. Pushing 200 changes unreviewed is the main risk.

- Landing is selectable: branch plus PR via `gh` by default, `--push-default` to commit to the default branch. PRs respect branch protection and run CI, but work only on GitHub.

## Workflow

A run is one named change across a selection of repos. It lives in a work directory until discarded.

```sh
repodb apply bump-checkout -s agents \
  --replace 'actions/checkout@v\d+' 'actions/checkout@v4' --glob '.github/workflows/*.y*ml' \
  -m "Bump actions/checkout to v4" -j 8
repodb review bump-checkout                # per-repo state and diffstat
repodb review bump-checkout --diff myra    # full diff for chosen repos
repodb publish bump-checkout               # push branch, open PR (default)
repodb publish bump-checkout --push-default
repodb runs                                # list runs and their state counts
repodb runs bump-checkout --discard        # delete the work directory
```

The run name (`bump-checkout`) is the key for every later step and the default branch name, prefixed `repodb/`.

## CLI

```sh
$ uv run repodb --help
usage: repodb [-h] [--db DB] COMMAND ...

Keep a database of git project URLs; clone, compare and change the
projects in bulk.

options:
  -h, --help  show this help message and exit
  --db DB     database file

commands:
  COMMAND
    scan      add origin URLs of projects in DIR
    github    add USER's GitHub repos (needs gh)
    import    add projects from a JSON file, flat or grouped
    export    write projects as {name: url} JSON
    list      print project names
    set       add projects to a named set, or list sets
    topics    print each topic and its project count
    status    compare DEST with the database
    remove    delete projects from the database; cloned directories
              are kept
    info      describe the database; read-only
    clone     clone projects into DEST
    apply     prepare a change across repos as local commits
    review    show a run's per-repo state
    publish   push a run's commits and open PRs
    runs      list runs, or discard one
```

- Selection reuses `select()` and `find()`, so `-s`, `-t`, `--owner` and `-r` mean what they mean in `clone`.

- `--exec CMD` runs through the shell, so globs work.

- `--replace PATTERN REPL --glob GLOB` is a built-in, Python `re` substitution over matching tracked files. It avoids shell and perl quoting: in `perl -pi -e 's/checkout@v3/.../'`, `@v3` interpolates as an array unless escaped. It also counts matches per file for `review`. About 25 lines; drop it if `--exec` proves enough.

- `-m MSG`: the first line is the commit subject and PR title; the rest is the PR body.

## Work directory

`~/.local/share/repodb/runs/RUN/`, next to the default database; `--workdir` overrides.

```
runs/.bump-checkout.lock    # held by apply, publish and runs --discard
runs/bump-checkout/
  run.json                  # manifest
  repos/OWNER/NAME/         # shallow clone
  logs/OWNER__NAME.log      # CMD output, git errors
```

`run.json` over tables in `repos.sqlite`: a run is temporary, per-change state. A file is inspectable and goes away with `--discard`. The database stays a list of projects.

```json
{
  "version": 1,
  "name": "bump-checkout",
  "message": "Bump actions/checkout to v4",
  "branch": "repodb/bump-checkout",
  "exec": null,
  "script": null,
  "replace": ["actions/checkout@v\\d+", "actions/checkout@v4"],
  "glob": [".github/workflows/*.y*ml"],
  "repos": {
    "shakfu/myra": {
      "url": "https://github.com/shakfu/myra",
      "state": "committed",
      "default_branch": "main",
      "base": "3f2a...", "commit": "9c1e...",
      "files": [".github/workflows/ci.yml"],
      "stat": "1 file changed, 1 insertion(+), 1 deletion(-)",
      "matches": {".github/workflows/ci.yml": 1},
      "error": null, "pr": null
    }
  }
}
```

The main thread writes the manifest after each repo finishes, to a temporary file then `os.replace`. A crash or Ctrl-C keeps finished repos.

`version` is raised only on a change older code would misread; such a run is refused as unreadable. Unknown keys are ignored, so an added field needs no bump.

The lock is a non-blocking `flock` (`msvcrt.locking` on Windows), held from loading the manifest to its last save. A second `apply`, `publish` or `runs --discard` on the same run exits instead of interleaving writes. The OS releases it when the process dies, so a crash leaves no stale lock. The file sits beside the run directory, not in it: a failed new `apply` creates no directory, and `--discard` can delete the directory while holding the lock, which Windows refuses for an open file. It is never deleted, since unlinking a lock file another process has open lets two processes hold it.

## Per-repo states

```
selected -> cloned -> unchanged                 (no diff; never published)
                   -> committed -> published    (branch pushed, PR open; or pushed to default)
         -> failed  (clone, CMD exit or timeout, commit, push; error recorded)
```

- `apply RUN` again skips repos past `cloned` and retries `failed` ones. A partial failure is resumed by rerunning the same command.

- `apply` refuses to continue a run whose `--exec`, `--replace` or `-m` differs from the manifest, unless `--redo`. `--redo [SPEC ...]` resets those repos (all if none are given) to `selected`, and records the new change.

- `publish` acts only on `committed`, and skips `published`, so it can be rerun too.

## Scripts

`apply --script FILE` copies FILE to `runs/RUN/script`, executable, and records its sha256 as `script`. Each repo runs that copy by absolute path, without a shell, so the `#!` line picks the interpreter.

- A copy over the path: `--exec` runs in each clone, so a relative path to a script does not resolve, and an edit between runs would reach only the repos resumed after it.

- The digest over the file's mtime: a resumed run with an edited FILE is a changed change and needs `--redo`. `apply RUN` without `--script` keeps the stored copy.

- FILE is read once, so the copy run is the one hashed. A file without `#!` is refused before cloning, since it would fail in every repo.

`repodb template sh|py` prints a starter. Both fail until edited, so an unedited template cannot commit anything. The Python one is stdlib only and is type-checked with the package.

## `apply`, per repo

1. `git clone --depth 1 --single-branch URL repos/OWNER/NAME`. Record the default branch (`HEAD`) and base commit. Your working copies are not touched: they can be dirty, on another branch, or behind origin.

2. Run the change with the repo root as cwd. `CMD` gets `REPODB_RUN`, `REPODB_OWNER`, `REPODB_NAME`, `REPODB_URL`. Output goes to the log. Non-zero exit or `--timeout` marks the repo `failed`.

3. `git status --porcelain` empty: `unchanged`.

4. `git switch -c BRANCH`, `git add -A`, `git commit -m MSG`. Record the commit and changed files. `git add -A` stages new files, which scripts may need, e.g. adding `dependabot.yml`. It also stages stray output such as `__pycache__`. `review` shows the file list so that is caught before publishing.

## `review`

One line per repo, grouped by state: `committed  shakfu/myra  1 file, +1 -1`. `--diff` prints `git show --stat --patch` for the chosen repos. With `--replace`, it also prints match counts, so a pattern that matched nowhere shows as 0.

## `publish`, per `committed` repo

Checks, once per run:

- If any commit touches `.github/workflows/`, check `gh auth status` lists the `workflow` scope, or stop with `gh auth refresh -s workflow`. Without it, GitHub rejects every such push.

Per repo:

1. `git fetch --depth 1 origin DEFAULT`. If the default branch moved since `base`, mark `failed` with "origin moved; apply --redo SPEC". Rebasing automatically could hide a conflict or a semantic clash.

2. PR mode (GitHub only; other hosts are skipped with a message):

   - `git push origin BRANCH`. If the remote branch exists at another commit, fail; `--force-with-lease` is not the default.

   - `gh pr create --base DEFAULT --head BRANCH --title SUBJECT --body BODY [--draft]`. Record the PR url. If creation fails, record an open PR for the same repo, base and head, so a retry after a lost response does not stay stuck.

3. `--push-default`: `git push origin BRANCH:DEFAULT`. Branch protection or a moved branch rejects it; record the error.

`publish -j` defaults to 1, since GitHub rate-limits content creation such as PRs.

## Code layout

`src/repodb/apply.py` is the library, stdlib only; `src/repodb/cli.py` holds `apply`, `review`, `publish` and `runs`.

- `Run`: a dataclass for `run.json`, with `create`, `load`, `save` (atomic), `add`, `resolve`, `redo` and `counts`.
- `apply_run(run, root, jobs, timeout, on_done)` and `publish_run(run, root, slugs, push_default, draft, jobs, on_done)` process repos in a thread pool, saving after each; `on_done` receives each repo's new state.
- `prepare` and `publish_one` do one repo each; `replace_in` returns match counts per file.

Estimate: 400-500 lines of code, a similar amount of tests.

## Tests

The `remotes` fixture already maps `https://git.example.com/OWNER/NAME.git` to local bare repos, so clones and pushes are real.

- `apply`: changed, unchanged, CMD failure, timeout, resume after failure, refusal on a changed command, `--redo`.

- `--replace`: counts, and no match gives `unchanged`.

- `publish --push-default`: the bare repo's default branch advances. Origin moved since apply: `failed`.

- PR mode: `gh` mocked; assert arguments; the remote branch is pushed.

- The manifest survives an exception partway through a run.

## Failure modes

- Missing `workflow` scope: checked before any push.

- Archived repos reject pushes; forks usually should not be changed. repodb stores neither flag, so `apply` cannot filter them. Either add the metadata (see TODO) or rely on the push failing and being recorded.

- Branch protection rejects `--push-default`; recorded per repo.

- HTTPS urls need a git credential helper for push, e.g. `gh auth setup-git`. SSH urls need a loaded key.

- Pushing from a shallow clone needs the server to have the base commit. GitHub does; a moved branch is caught at step 1.

- 200 clones at `--depth 1` cost disk and time. `-j` for `apply` can be high; `publish -j` should stay low.

## Decisions from review

- Branches are `repodb/RUN`; `--branch` overrides it when the run is created, and it is fixed afterwards.

- `git add -A`: new files are staged.

- `--replace` is kept.

- PR tracking (`runs RUN --prs`) is deferred; see TODO.

## Notes from implementation

- A new run needs an explicit selection (`-r`, `-s`, `-t`, `--owner` or `--all`), so a missing filter cannot select every project.

- A failed publish leaves the repo `committed` with the error, so `publish` retries it. A failed apply marks it `failed`, so `apply` retries it.

- `--replace` writes only tracked regular files: symlinks are skipped, since their target can be outside the repo, as are files that are not UTF-8. Globs that are absolute or contain `..` are rejected. CRLF line endings are kept.

- `--exec` runs in its own session, so `--timeout` kills the command's children too. Ctrl-C does not reach them; they finish or time out.

- Commits use your git identity. Set `GIT_AUTHOR_*`/`GIT_COMMITTER_*` or `includeIf` config if these repos need another one.

- Commits also use your signing config. With `commit.gpgsign = true`, each repo's commit signs separately, so a key without a cached passphrase prompts once per repo. With `-j N`, N prompts can be pending at once. Cache the passphrase first, e.g. with `gpg-agent`, or disable signing for the run: `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=commit.gpgsign GIT_CONFIG_VALUE_0=false repodb apply ...`.
