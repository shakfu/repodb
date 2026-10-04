"""Change many repos at once: `apply_run` prepares commits, `publish_run` lands them.

A run is a named change (a shell command, or a regex substitution over files)
and the repos it applies to. It is kept in a directory, by default
``runs/RUN/`` next to the database: ``run.json``, a shallow clone per repo
under ``repos/OWNER/NAME``, and a log per repo. Calling `apply_run` or
`publish_run` again resumes it. `locked` keeps other processes out.

    run = Run.create("bump", "Bump checkout", replace=[r"checkout@v\\d+", "checkout@v5"],
                     glob=[".github/workflows/*.yml"])
    run.add(db.rows(set_name="agents"))
    root = runs_dir(db.db_path) / run.name
    with locked(root):
        apply_run(run, root, jobs=8)  # review run.repos, then
        publish_run(run, root)

Design and failure modes: ``docs/dev/apply.md``. The command-line interface
is `repodb.cli`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

from repodb.core import Row, host_of, owner_of, repo_of, url_key, valid_name

log = logging.getLogger(__name__)

RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # a path and branch component
STATES = ("committed", "published", "unchanged", "failed", "selected")
RUN_VERSION = 1  # of run.json; raise on a change older code would misread

OnDone = Callable[[str, "Repo"], None]


class Failed(Exception):
    """A step failed for one repo; the message is recorded in the run."""


class Locked(Exception):
    """Another process holds the run's lock."""


def known(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Return the items of *data* that are fields of dataclass *cls*."""
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in names}


if sys.platform == "win32":
    import msvcrt

    def try_lock(f: IO[str]) -> bool:
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

else:
    import fcntl

    def try_lock(f: IO[str]) -> bool:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


@contextmanager
def locked(root: Path) -> Iterator[None]:
    """Hold the lock of run directory *root*, ``.NAME.lock`` beside it.

    Hold it from `Run.load` to the last `Run.save`, so two processes cannot
    interleave changes to one run. The lock is outside *root*, so `discard`
    can remove *root* while holding it.

    Raises:
        Locked: if another process holds it.
    """
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent / f".{root.name}.lock").open("a+") as f:
        f.seek(0)  # msvcrt locks from the file position
        if not try_lock(f):
            raise Locked(f"run {root.name!r} is in use by another process")
        yield  # the lock is released when f closes


def check_run_name(name: str) -> None:
    """Raises ValueError unless *name* is usable as a directory and branch part."""
    if not RUN_NAME.match(name):
        raise ValueError(
            f"invalid run name {name!r}: use letters, digits, '.', '_', '-'"
        )


def check_slug(slug: str) -> bool:
    """True if *slug* is ``OWNER/NAME``, each a single path component."""
    owner, _, name = slug.partition("/")
    return valid_name(owner) and valid_name(name)


def check_change(
    exec: str | None, replace: Sequence[str] | None, glob: Sequence[str]
) -> None:
    """Raises ValueError if the change is not one valid command or substitution."""
    if exec is not None and replace:
        raise ValueError("give exec or replace, not both")
    if bool(replace) != bool(glob):
        raise ValueError("replace and glob go together")
    for g in glob:
        if Path(g).is_absolute() or ".." in Path(g).parts:
            raise ValueError(f"glob must stay inside the repo: {g!r}")
    if replace:
        if len(replace) != 2:
            raise ValueError("replace is [pattern, replacement]")
        try:
            re.compile(replace[0])
        except re.error as e:
            raise ValueError(f"replace pattern: {e}") from e


def runs_dir(db_path: Path) -> Path:
    """Return the default directory holding runs, next to the database."""
    return Path(db_path).parent / "runs"


@dataclass
class Repo:
    """One repo's progress through a run."""

    url: str
    state: str = "selected"
    default_branch: str | None = None
    base: str | None = None
    commit: str | None = None
    files: list[str] = field(default_factory=list)
    stat: str = ""
    matches: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    pr: str | None = None


@dataclass
class Run:
    """A named change and the repos it applies to, stored as ``run.json``."""

    name: str
    message: str
    branch: str
    exec: str | None = None
    replace: list[str] | None = None  # [pattern, replacement]
    glob: list[str] = field(default_factory=list)
    repos: dict[str, Repo] = field(default_factory=dict)  # OWNER/NAME -> Repo

    @classmethod
    def create(
        cls,
        name: str,
        message: str,
        exec: str | None = None,
        replace: Sequence[str] | None = None,
        glob: Sequence[str] = (),
        branch: str | None = None,
    ) -> Run:
        """Return a new run; its branch defaults to ``repodb/NAME``.

        Raises:
            ValueError: on an invalid name, change or empty message.
        """
        check_run_name(name)
        check_change(exec, replace, glob)
        if exec is None and not replace:
            raise ValueError("a run needs exec or replace")
        if not message:
            raise ValueError("a run needs a commit message")
        return cls(
            name,
            message,
            branch or f"repodb/{name}",
            exec,
            list(replace) if replace else None,
            list(glob),
        )

    @classmethod
    def load(cls, root: Path) -> Run:
        """Read ``root/run.json``.

        Raises:
            FileNotFoundError: if there is no run in *root*.
            ValueError, TypeError, KeyError: if ``run.json`` is malformed or
                from a newer format. Unknown keys are ignored.
        """
        data = json.loads((root / "run.json").read_text())
        if (version := data.get("version", 1)) > RUN_VERSION:
            raise ValueError(f"run.json version {version} is newer than {RUN_VERSION}")
        repos = {k: Repo(**known(Repo, v)) for k, v in data["repos"].items()}
        return cls(**known(cls, data) | {"repos": repos})

    def save(self, root: Path) -> None:
        """Write ``run.json`` atomically, so a crash keeps the previous state."""
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / "run.json.tmp"
        data = {"version": RUN_VERSION} | asdict(self)
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, root / "run.json")

    def change(self) -> tuple[object, ...]:
        return (self.exec, self.replace, self.glob, self.message)

    def set_change(
        self,
        message: str,
        exec: str | None = None,
        replace: Sequence[str] | None = None,
        glob: Sequence[str] = (),
    ) -> None:
        """Replace the change and message; `redo` the repos it should apply to.

        Raises:
            ValueError: on an invalid change.
        """
        check_change(exec, replace, glob)
        self.exec, self.message = exec, message
        self.replace, self.glob = (list(replace) if replace else None), list(glob)

    def add(self, rows: Iterable[Row]) -> list[str]:
        """Add ``(owner, name, url)`` rows not yet in the run; return their slugs.

        A row whose url `url_key` matches a repo already in the run is skipped.

        Raises:
            ValueError: if an owner or name is not a single path component;
                nothing is added.
        """
        rows = list(rows)
        if bad := [f"{o}/{n}" for o, n, _ in rows if not check_slug(f"{o}/{n}")]:
            raise ValueError(f"invalid owner or name: {', '.join(map(repr, bad))}")
        seen = {url_key(r.url): s for s, r in self.repos.items()}
        added = []
        for o, n, u in rows:
            slug = f"{o}/{n}"
            if (first := seen.setdefault(url_key(u), slug)) != slug:
                log.warning("same repo as %s, skipping: %s (%s)", first, slug, u)
            elif slug not in self.repos:
                self.repos[slug] = Repo(url=u)
                added.append(slug)
        return added

    def resolve(self, specs: Iterable[str]) -> list[str]:
        """Return the slugs for ``OWNER/NAME`` or bare ``NAME`` specs.

        Raises:
            ValueError: if a spec matches no repo in the run, or several.
        """
        slugs = []
        for spec in specs:
            key = spec.lower()
            matches = [
                s
                for s in self.repos
                if (s.lower() if "/" in spec else s.split("/")[1].lower()) == key
            ]
            if not matches:
                raise ValueError(f"{spec!r}: not in run {self.name!r}")
            if len(matches) > 1:
                raise ValueError(
                    f"{spec!r}: ambiguous, use one of {', '.join(matches)}"
                )
            slugs.append(matches[0])
        return slugs

    def redo(self, slugs: Iterable[str] | None = None) -> None:
        """Reset *slugs* (default: all) so `apply_run` prepares them again.

        Raises:
            ValueError: if any is published; nothing is reset.
        """
        slugs = list(self.repos) if slugs is None else list(slugs)
        if published := [s for s in slugs if self.repos[s].state == "published"]:
            raise ValueError(f"already published: {', '.join(published)}")
        for s in slugs:
            self.repos[s] = Repo(url=self.repos[s].url)

    def counts(self) -> dict[str, int]:
        """Return the number of repos in each state, in `STATES` order."""
        counts = {s: 0 for s in STATES}
        for r in self.repos.values():
            counts[r.state] += 1
        return {s: n for s, n in counts.items() if n}


def git(repo: Path, *args: str) -> str:
    """Run git in *repo* and return its stripped stdout.

    Raises:
        Failed: if git exits non-zero, with its stderr.
    """
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if r.returncode:
        raise Failed(f"git {args[0]}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def replace_in(
    root: Path, pattern: re.Pattern[str], repl: str, globs: Iterable[str]
) -> dict[str, int]:
    """Substitute *pattern* in tracked UTF-8 files matching *globs*; return counts.

    Symlinks are skipped, since their target can be outside *root*. Line
    endings are kept.

    Raises:
        Failed: if *repl* is invalid for *pattern*.
    """
    tracked = set(git(root, "ls-files", "-z").split("\0"))
    counts: dict[str, int] = {}
    for g in globs:
        for p in sorted(root.glob(g)):
            rel = p.relative_to(root).as_posix()
            if rel in counts or rel not in tracked or p.is_symlink() or not p.is_file():
                continue
            try:
                text = p.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue
            try:
                new, n = pattern.subn(repl, text)
            except re.error as e:
                raise Failed(f"replace: {e}") from e
            if n:
                p.write_bytes(new.encode("utf-8"))
                counts[rel] = n
    return counts


def run_exec(
    cmd: str, cwd: Path, env: dict[str, str], out: IO[str], timeout: int | None
) -> None:
    """Run *cmd* through the shell in *cwd*, output to *out*.

    Raises:
        Failed: on a non-zero exit or timeout.
    """
    # A new session, so a timeout can kill the command's children too.
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        env=env,
        stdout=out,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise Failed(f"exec timed out after {timeout}s") from None
    if rc:
        raise Failed(f"exec exited {rc}; see {out.name}")


def prepare(run: Run, slug: str, url: str, root: Path, timeout: int | None) -> Repo:
    """Clone, change and commit one repo; return its new state."""
    d = root / "repos" / slug
    logfile = root / "logs" / f"{slug.replace('/', '__')}.log"
    new = Repo(url=url)
    try:
        # run.json can be edited by hand; keep d inside root before rmtree.
        if not check_slug(slug):
            raise Failed(f"invalid owner or name: {slug!r}")
        if d.exists():
            shutil.rmtree(d)
        d.parent.mkdir(parents=True, exist_ok=True)
        logfile.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--depth",
                "1",
                "--single-branch",
                "--",
                url,
                str(d),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode:
            raise Failed(f"git clone: {r.stderr.strip()}")
        new.default_branch = git(d, "symbolic-ref", "--short", "HEAD")
        new.base = git(d, "rev-parse", "HEAD")
        with logfile.open("w") as out:
            if run.exec is not None:
                owner, name = slug.split("/")
                env = os.environ | {
                    "REPODB_RUN": run.name,
                    "REPODB_OWNER": owner,
                    "REPODB_NAME": name,
                    "REPODB_URL": url,
                }
                run_exec(run.exec, d, env, out, timeout)
            elif run.replace is not None:
                pattern, repl = run.replace
                new.matches = replace_in(d, re.compile(pattern), repl, run.glob)
                out.writelines(f"{n} {f}\n" for f, n in new.matches.items())
        if not git(d, "status", "--porcelain"):
            new.state = "unchanged"
            return new
        git(d, "checkout", "-q", "-b", run.branch)
        # -A stages new files, which a change may need; review lists them.
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", run.message)
        new.commit = git(d, "rev-parse", "HEAD")
        new.files = git(d, "diff", "--name-only", new.base, new.commit).splitlines()
        new.stat = git(d, "diff", "--shortstat", new.base, new.commit)
        new.state = "committed"
    except Failed as e:
        new.state, new.error = "failed", str(e)
    except Exception as e:  # e.g. OSError; one repo must not abort the run
        log.exception("%s: unexpected error", slug)
        new.state, new.error = "failed", f"{type(e).__name__}: {e}"
    return new


def publish_one(
    run: Run, slug: str, repo: Repo, root: Path, push_default: bool, draft: bool
) -> Repo:
    """Push one committed repo, opening a PR unless *push_default*.

    A failure keeps the repo ``committed`` with the error, so publish can retry.
    """
    d = root / "repos" / slug
    new = dataclasses.replace(repo, error=None)
    default = repo.default_branch or ""
    try:
        if not push_default and host_of(repo.url) != "github.com":
            raise Failed("PR mode needs a github.com url; push to the default branch")
        git(d, "fetch", "-q", "--depth", "1", "origin", default)
        # Rebasing could hide a conflict; redoing the change is explicit.
        if git(d, "rev-parse", "FETCH_HEAD") != repo.base:
            raise Failed(f"origin {default} moved since apply; redo {slug}")
        if push_default:
            git(d, "push", "-q", "origin", f"{run.branch}:{default}")
        else:
            git(d, "push", "-q", "origin", run.branch)
            subject, _, body = run.message.partition("\n")
            where = ["--repo", f"{owner_of(repo.url)}/{repo_of(repo.url)}"]
            where += ["--base", default, "--head", run.branch]
            cmd = ["gh", "pr", "create", *where]
            cmd += ["--title", subject, "--body", body.strip()]
            if draft:
                cmd.append("--draft")
            r = subprocess.run(cmd, cwd=d, capture_output=True, text=True, check=False)
            if r.returncode:
                # An earlier publish may have opened the PR but not recorded it.
                found = subprocess.run(
                    ["gh", "pr", "list", *where, "--json", "url", "-q", ".[0].url"],
                    cwd=d,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if found.returncode or not found.stdout.strip():
                    raise Failed(f"gh pr create: {r.stderr.strip()}")
                r = found
            new.pr = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else None
        new.state = "published"
    except Failed as e:
        new.error = str(e)
    except Exception as e:  # e.g. gh not installed, after the push
        log.exception("%s: unexpected error", slug)
        new.error = f"{type(e).__name__}: {e}"
    return new


def parallel(
    slugs: list[str],
    work: Callable[[str], Repo],
    jobs: int,
    done: Callable[[str, Repo], None],
) -> None:
    """Run *work* on each slug, *jobs* at once, calling *done* in this thread."""
    pool = ThreadPoolExecutor(jobs)
    try:
        futures = {pool.submit(work, s): s for s in slugs}
        for f in as_completed(futures):
            done(futures[f], f.result())
    except BaseException:  # Ctrl-C, or an error in *done*
        pool.shutdown(cancel_futures=True)
        raise
    pool.shutdown()


def _run_all(
    run: Run,
    root: Path,
    slugs: list[str],
    work: Callable[[str], Repo],
    jobs: int,
    on_done: OnDone | None,
) -> None:
    run.save(root)

    def done(slug: str, repo: Repo) -> None:
        run.repos[slug] = repo
        run.save(root)  # after each repo, so an interruption keeps progress
        if on_done is not None:
            on_done(slug, repo)

    parallel(slugs, work, jobs, done)


def apply_run(
    run: Run,
    root: Path,
    jobs: int = 1,
    timeout: int | None = None,
    on_done: OnDone | None = None,
) -> list[str]:
    """Prepare each ``selected`` or ``failed`` repo; return the slugs tried.

    *root* is the run's directory. *on_done* is called with each repo's new
    state as it finishes.
    """
    slugs = [s for s, r in run.repos.items() if r.state in ("selected", "failed")]
    _run_all(
        run,
        root,
        slugs,
        lambda s: prepare(run, s, run.repos[s].url, root, timeout),
        jobs,
        on_done,
    )
    return slugs


def needs_workflow_scope(run: Run, slugs: Iterable[str]) -> bool:
    """True if pushing *slugs* sends workflow changes to github.com over HTTPS.

    GitHub rejects such a push unless the token has the ``workflow`` scope.
    """
    return any(
        run.repos[s].url.startswith("https://github.com/")
        and any(f.startswith(".github/workflows/") for f in run.repos[s].files)
        for s in slugs
    )


def has_workflow_scope() -> bool:
    """True if ``gh auth status`` lists the ``workflow`` scope."""
    r = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, check=False
    )
    return "workflow" in r.stdout + r.stderr


def publish_run(
    run: Run,
    root: Path,
    slugs: Iterable[str] | None = None,
    push_default: bool = False,
    draft: bool = False,
    jobs: int = 1,
    on_done: OnDone | None = None,
) -> list[str]:
    """Push each ``committed`` repo of *slugs* (default: all); return those tried.

    Opens a PR per repo via ``gh`` unless *push_default*. A repo that fails
    stays ``committed`` with its error, so calling this again retries it.
    """
    candidates = list(run.repos) if slugs is None else list(slugs)
    todo = [s for s in candidates if run.repos[s].state == "committed"]
    _run_all(
        run,
        root,
        todo,
        lambda s: publish_one(run, s, run.repos[s], root, push_default, draft),
        jobs,
        on_done,
    )
    return todo


def diff(root: Path, slug: str, repo: Repo) -> str:
    """Return ``git show --stat --patch`` of *repo*'s commit, or '' if none."""
    if not repo.commit:
        return ""
    return subprocess.run(
        [
            "git",
            "-C",
            str(root / "repos" / slug),
            "show",
            "--stat",
            "--patch",
            repo.commit,
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def list_runs(base: Path) -> list[tuple[str, Run | Exception]]:
    """Return ``(name, run)`` for each run under *base*, or the load error."""
    runs: list[tuple[str, Run | Exception]] = []
    for p in sorted(base.glob("*/run.json")):
        try:
            runs.append((p.parent.name, Run.load(p.parent)))
        except (ValueError, TypeError, KeyError) as e:
            runs.append((p.parent.name, e))
    return runs


def discard(root: Path) -> None:
    """Delete the run directory *root*, clones and all."""
    shutil.rmtree(root)
