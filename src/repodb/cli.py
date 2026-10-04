"""Command-line interface for `repodb.core` and `repodb.apply`.

    repodb scan ~/src                        # add local projects' origin URLs
    repodb github USER                       # add USER's GitHub repos (needs gh)
    repodb github --refresh [USER] [--prune [--yes]]  # update stored GitHub repos
    repodb export [--owner USER] [-s SET] [-t TOPIC] -o projects.json
    repodb import projects.json
    repodb list [--urls | --slugs] [--group] [--topics] [filters]
    repodb topics [--owner USER]
    repodb set [--name SET [SPEC ...] [--remove | --delete]]
    repodb clone ~/src [--group] [-j N] [filters | -r SPEC ... | --json FILE] [-- GIT_OPTION ...]
    repodb status ~/src [--group] [filters]
    repodb remove OWNER/NAME [NAME ...] | --owner USER --all
    repodb info
    repodb apply | review | publish | runs

Filters are ``--owner USER``, ``-s SET`` and ``-t TOPIC [-t ...] [--any]``.
Handlers print results, turn library exceptions into messages, and return the
exit status; library log records go to stdout (info) or stderr (warnings).
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path

from repodb import apply as ap
from repodb.core import (
    DB_PATH,
    GitRepoDB,
    Row,
    clone,
    export_projects,
    flatten,
    github,
    has_topics,
    import_projects,
    info,
    owner_of,
    read_projects,
    refresh,
    repo_of,
    scan,
    status,
    to_json,
)

SRC_DIR = Path("~/src").expanduser()

Handler = Callable[[argparse.Namespace, argparse.ArgumentParser], int]


class Console(logging.Handler):
    """Print log records: info to stdout, warnings and above to stderr."""

    def emit(self, record: logging.LogRecord) -> None:
        # Looked up per record, so redirected streams (and tests) are honoured.
        stream = sys.stderr if record.levelno >= logging.WARNING else sys.stdout
        print(self.format(record), file=stream)


def open_db(args: argparse.Namespace) -> GitRepoDB:
    """Open ``args.db``, exiting with status 1 if its schema is unsupported."""
    try:
        return GitRepoDB(args.db)
    except ValueError as e:
        raise SystemExit(str(e)) from None


def select(db: GitRepoDB, args: argparse.Namespace) -> list[Row]:
    """Return the rows matching ``--owner``, ``-s`` and ``-t``."""
    return db.rows(args.owner, args.topic or (), args.any, args.set_name)


def no_match(args: argparse.Namespace, where: str | None = None) -> int:
    """Report that the ``-s`` and ``-t`` filters matched nothing; return 1."""
    parts = []
    if args.set_name:
        parts.append(f"in set {args.set_name!r}")
    if args.topic:
        topics = (" or " if args.any else " and ").join(map(repr, args.topic))
        parts.append(f"with topic{'s' if len(args.topic) > 1 else ''} {topics}")
    if where is None:
        where = (
            "; see 'repodb set'"
            if args.set_name
            else "; topics are added by 'repodb github'"
        )
    print(f"no projects {' '.join(parts)}{where}", file=sys.stderr)
    return 1


def check_flat(parser: argparse.ArgumentParser, names: list[str]) -> None:
    """Exit with a usage error if two *names* would share ``DEST/<name>``."""
    try:
        flatten((n, None) for n in names)
    except ValueError as e:
        parser.error(f"{e}; use --group")


def print_all(items: Sequence[object]) -> None:
    for item in items:
        print(item)


def cmd_scan(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not args.directory.is_dir():
        parser.error(f"not a directory: {args.directory}")
    with open_db(args) as db:
        print_all(db.add(scan(args.directory)))
    return 0


def cmd_github(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.yes and not args.prune:
        parser.error("--yes confirms --prune")
    if args.prune and not args.refresh:
        parser.error("--prune goes with --refresh")
    if args.refresh:
        if args.ssh or args.source or args.no_archived:
            parser.error(
                "--refresh updates only topics; drop --ssh, --source and --no-archived"
            )
    elif args.user is None:
        parser.error("give USER, or --refresh to update stored repos' topics")
    if shutil.which("gh") is None:
        parser.error("github requires the gh CLI: https://cli.github.com")
    if args.refresh:
        with open_db(args) as db:
            return report_refresh(db, args)
    try:
        projects = github(
            args.user, args.limit, args.ssh, args.source, args.no_archived
        )
    except subprocess.CalledProcessError as e:
        print(f"gh failed: {e.stderr.strip()}", file=sys.stderr)
        return 1
    with open_db(args) as db:
        print_all(db.add_projects((n, u, list(t)) for n, u, t in projects))
    return 0


def report_refresh(db: GitRepoDB, args: argparse.Namespace) -> int:
    """Run `refresh`, print its findings, and with ``--prune --yes`` remove."""
    r = refresh(db, args.user, args.limit)
    if not r.checked:
        print("no github.com projects to refresh", file=sys.stderr)
        return 1
    for owner, err in r.failed.items():
        print(f"gh failed for {owner}: {err}", file=sys.stderr)
    if args.prune:
        for owner, count in r.partial.items():
            print(
                f"gh listed {count} repos for {owner}{', the --limit' if count else ''};"
                " not pruning it",
                file=sys.stderr,
            )
    prune = r.prunable if args.prune else []
    for row in r.unlisted:
        if row not in prune:
            print(
                f"not listed by gh, topics unchanged: {row[0]}/{row[1]}",
                file=sys.stderr,
            )
    print_all(r.changes)
    if prune and not args.yes:
        for o, n, u in prune:
            print(f"not listed by gh, would remove: {o}/{n} <- {u}")
        print("add --yes to remove them", file=sys.stderr)
    elif prune:
        for o, n, u in db.remove(f"{o}/{n}" for o, n, _ in prune):
            print(f"removed: {o}/{n} <- {u}")
    return 1 if r.failed else 0


def cmd_import(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    with open_db(args) as db:
        try:
            print_all(import_projects(db, args.json))
        except (OSError, ValueError) as e:  # json.JSONDecodeError is a ValueError
            parser.error(f"{args.json}: {e}")
    return 0


def cmd_export(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    with open_db(args) as db:
        try:
            projects = export_projects(
                db, args.group, args.owner, args.topic or (), args.any, args.set_name
            )
        except ValueError as e:
            parser.error(f"{e}; use --group")
    if (args.topic or args.set_name) and not projects:
        return no_match(args)
    text = to_json(projects)
    if str(args.output) == "-":
        sys.stdout.write(text)
    else:
        args.output.write_text(text)
    return 0


def cmd_list(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    with open_db(args) as db:
        rows = select(db, args)
        tags = db.topics() if args.topics else {}
    if (args.topic or args.set_name) and not rows:
        return no_match(args)
    if not rows:
        print("no projects; run 'repodb scan' or 'repodb github USER'", file=sys.stderr)
        return 1
    # A slug comes from the url, since scan stores the directory name.
    labels = {
        (o, n): u if args.urls else f"{owner_of(u)}/{repo_of(u)}" if args.slugs else n
        for o, n, u in rows
    }
    width = max(map(len, labels.values()))

    def line(owner: str, name: str) -> str:
        label = labels[owner, name]
        if not args.topics:
            return label
        t = tags.get((owner.lower(), name.lower()), [])
        return f"{label:<{width}}  {', '.join(t)}".rstrip()

    if args.group:
        spelling: dict[str, str] = {}  # one group per owner; first spelling wins
        groups: dict[str, list[str]] = {}
        for o, n, _ in rows:
            groups.setdefault(spelling.setdefault(o.lower(), o), []).append(line(o, n))
        for owner, lines in groups.items():
            print(owner)
            print("\n".join(f"  {x}" for x in lines))
    else:
        flat = [line(o, n) for o, n, _ in rows]
        # Two rows can name one repo, e.g. a scanned clone; print its slug once.
        print("\n".join(dict.fromkeys(flat) if args.slugs else flat))
    return 0


def cmd_topics(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    with open_db(args) as db:
        counts = db.topic_counts(args.owner)
    if not counts:
        print("no topics; topics are added by 'repodb github'", file=sys.stderr)
        return 1
    for topic, n in counts:
        print(f"{n:>5}  {topic}")
    return 0


def cmd_status(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not args.dest.is_dir():
        parser.error(f"not a directory: {args.dest}")
    with open_db(args) as db:
        rows = select(db, args)
        every = db.rows()
    if (args.topic or args.set_name) and not rows:
        return no_match(args)
    found = status(rows, args.dest, args.group, known=every)
    print_all(found)
    return 1 if found else 0


def cmd_set(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.name is None:
        if args.specs or args.remove or args.delete:
            parser.error("SPEC, --remove and --delete need --name SET")
        with open_db(args) as db:
            for name, size in db.sets():
                print(f"{size:>5}  {name}")
        return 0
    if args.remove and args.delete:
        parser.error("give --remove or --delete, not both")
    if args.delete and args.specs:
        parser.error("--delete takes no SPEC")
    if args.remove and not args.specs:
        parser.error("--remove needs SPEC ...")
    with open_db(args) as db:
        try:
            if args.delete:
                size = db.set_delete(args.name)
                print(f"deleted set {args.name!r} ({size} projects)")
                return 0
            if not args.specs:
                rows = db.rows(set_name=args.name)
                if not rows:
                    raise ValueError(f"{args.name!r}: no such set")
                print("\n".join(f"{o}/{n}" for o, n, _ in rows))
                return 0
            if args.remove:
                for o, n, _ in db.set_remove(args.name, args.specs):
                    print(f"removed from {args.name}: {o}/{n}")
                return 0
            for o, n, _ in db.set_add(args.name, args.specs):
                print(f"added to {args.name}: {o}/{n}")
        except ValueError as e:
            print(f"{e}; nothing changed", file=sys.stderr)
            return 1
    return 0


def cmd_remove(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    # One flag must not delete many rows, so --owner needs --all as well.
    if bool(args.specs) == bool(args.owner):
        parser.error("give SPEC ... or --owner OWNER --all, not both or neither")
    if bool(args.owner) != args.all:
        parser.error("--owner and --all go together")
    with open_db(args) as db:
        try:
            removed = (
                db.remove_owner(args.owner) if args.owner else db.remove(args.specs)
            )
        except ValueError as e:
            print(f"{e}; nothing removed", file=sys.stderr)
            return 1
    for o, n, u in removed:
        print(f"removed: {o}/{n} <- {u}")
    return 0


def cmd_info(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not args.db.is_file():
        print(f"no database at {args.db}", file=sys.stderr)
        return 1
    try:
        fields = info(args.db)
    except sqlite3.Error as e:
        print(f"{args.db}: {e}", file=sys.stderr)
        return 1
    for k, v in fields:
        print(f"{k + ':':<14}{v}")
    return 0


def cmd_clone(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if (args.owner or args.set_name) and (args.json or args.repo):
        parser.error(
            "--owner and -s filter the database; they do not apply with --json or -r"
        )
    if args.repo and args.topic:
        parser.error("-r and -t do not go together")
    rows: list[tuple[str | None, str, str]]
    if args.repo:
        with open_db(args) as db:
            try:
                # dict.fromkeys drops a repeated spec, which flatten would reject.
                rows = list(dict.fromkeys(db.find(s) for s in args.repo))
            except ValueError as e:
                print(e, file=sys.stderr)
                return 1
    elif args.json:
        try:
            projects = read_projects(args.json)
        except (OSError, ValueError) as e:
            parser.error(f"{args.json}: {e}")
        if args.topic:
            projects = [p for p in projects if has_topics(p[2], args.topic, args.any)]
            if not projects:
                return no_match(args, f" in {args.json}")
        rows = [(owner_of(u), n, u) for n, u, _ in projects]
    else:
        with open_db(args) as db:
            rows = list(select(db, args))
        if (args.topic or args.set_name) and not rows:
            return no_match(args)
    if not args.group:
        check_flat(parser, [n for _, n, _ in rows])
    try:
        failed = clone(
            rows,
            args.dest,
            args.group,
            args.jobs,
            args.git_options,
            live=args.jobs == 1,
        )
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


# apply, review, publish, runs


def run_root(args: argparse.Namespace) -> Path:
    base: Path = args.workdir if args.workdir is not None else ap.runs_dir(args.db)
    root: Path = base / args.run
    return root


def load_run(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[ap.Run, Path]:
    """Load run ``args.run``, exiting with status 1 if there is none."""
    try:
        ap.check_run_name(args.run)
    except ValueError as e:
        parser.error(str(e))
    root = run_root(args)
    try:
        return ap.Run.load(root), root
    except FileNotFoundError:
        raise SystemExit(f"no run {args.run!r} in {root.parent}") from None
    except (ValueError, TypeError, KeyError) as e:
        raise SystemExit(f"{root / 'run.json'}: unreadable: {e}") from None


@contextmanager
def hold(
    args: argparse.Namespace, parser: argparse.ArgumentParser, create: bool = False
) -> Iterator[Path]:
    """Lock run ``args.run`` and yield its directory; exit 1 if it is locked.

    Without *create*, a missing run is not locked, so `load_run` reports it.
    """
    try:
        ap.check_run_name(args.run)
    except ValueError as e:
        parser.error(str(e))
    root = run_root(args)
    with ExitStack() as stack:
        if create or root.is_dir():
            try:
                stack.enter_context(ap.locked(root))
            except ap.Locked as e:
                raise SystemExit(str(e)) from None
        yield root


def detail(repo: ap.Repo) -> str:
    """What happened to *repo*, for review and progress lines."""
    if repo.state == "committed":
        text = repo.stat
        if repo.matches:
            text += f"; {sum(repo.matches.values())} matches"
        if repo.error:
            text += f"; publish failed: {repo.error}"
        return text
    if repo.state == "published":
        return repo.pr or f"pushed to {repo.default_branch}"
    if repo.state == "failed":
        return repo.error or ""
    return ""


def report_line(slug: str, repo: ap.Repo) -> None:
    """Print one progress line; failures go to stderr."""
    text = f"{repo.state}: {slug}" + (f" ({d})" if (d := detail(repo)) else "")
    print(text, file=sys.stderr if repo.state == "failed" or repo.error else sys.stdout)


def summary(run: ap.Run) -> str:
    return ", ".join(f"{s} {n}" for s, n in run.counts().items()) or "no repos"


def cmd_apply(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        ap.check_change(args.exec, args.replace, args.glob or [])
    except ValueError as e:
        parser.error(str(e))
    with hold(args, parser, create=True) as root:
        exists = (root / "run.json").exists()
        if exists:
            run, _ = load_run(args, parser)
            if args.branch and args.branch != run.branch:
                parser.error(
                    f"run {run.name!r} uses branch {run.branch!r}; it is fixed per run"
                )
            if args.exec is not None or args.replace:
                change = (args.exec, args.replace, args.glob or [])
            else:
                change = (run.exec, run.replace, run.glob)
            message = args.message or run.message
            if (*change, message) != run.change():
                if args.redo is None:
                    parser.error(
                        f"the change or message differs from run {run.name!r};"
                        " add --redo to apply it"
                    )
                run.set_change(message, *change)
        else:
            if args.exec is None and not args.replace:
                parser.error("a new run needs --exec or --replace")
            if not args.message:
                parser.error("a new run needs -m MESSAGE")
            run = ap.Run.create(
                args.run,
                args.message,
                args.exec,
                args.replace,
                args.glob or [],
                args.branch,
            )
        if args.repo or args.set_name or args.topic or args.owner or args.all:
            with open_db(args) as db:
                try:
                    rows = (
                        [db.find(s) for s in args.repo]
                        if args.repo
                        else select(db, args)
                    )
                except ValueError as e:
                    print(e, file=sys.stderr)
                    return 1
            if (args.topic or args.set_name) and not rows:
                return no_match(args)
            if not rows:
                owned = f" owned by {args.owner!r}" if args.owner else ""
                print(f"no projects{owned}; nothing to apply", file=sys.stderr)
                return 1
            try:
                run.add(rows)
            except ValueError as e:
                print(e, file=sys.stderr)
                return 1
        elif not exists:
            parser.error("select repos with -r, -s, -t, --owner or --all")
        if args.redo is not None:
            try:
                run.redo(run.resolve(args.redo) if args.redo else None)
            except ValueError as e:
                print(e, file=sys.stderr)
                return 1
        ap.apply_run(run, root, args.jobs, args.timeout, on_done=report_line)
        print(f"run {run.name}: {summary(run)}; review with 'repodb review {run.name}'")
        return 1 if run.counts().get("failed") else 0


def cmd_review(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    run, root = load_run(args, parser)
    try:
        slugs = run.resolve(args.specs) if args.specs else sorted(run.repos)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    for state in ap.STATES:
        for s in slugs:
            repo = run.repos[s]
            if repo.state == state:
                print(f"{repo.state:<10} {s}  {detail(repo)}".rstrip())
    if args.diff:
        for s in slugs:
            if text := ap.diff(root, s, run.repos[s]):
                print(f"\n== {s}")
                sys.stdout.write(text)
    return 0


def cmd_publish(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    with hold(args, parser):
        run, root = load_run(args, parser)
        try:
            slugs = run.resolve(args.specs) if args.specs else list(run.repos)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        todo = [s for s in slugs if run.repos[s].state == "committed"]
        if not todo:
            print(
                f"run {run.name}: nothing to publish ({summary(run)})", file=sys.stderr
            )
            return 0
        gh = shutil.which("gh")
        if not args.push_default and gh is None:
            parser.error(
                "PR mode needs the gh CLI: https://cli.github.com; or --push-default"
            )
        if (
            gh is not None
            and ap.needs_workflow_scope(run, todo)
            and not ap.has_workflow_scope()
        ):
            print(
                "changes touch .github/workflows/ but the gh token lacks the 'workflow'"
                " scope; run 'gh auth refresh -s workflow'",
                file=sys.stderr,
            )
            return 1
        tried = ap.publish_run(
            run, root, todo, args.push_default, args.draft, args.jobs, report_line
        )
        print(f"run {run.name}: {summary(run)}")
        return 1 if any(run.repos[s].error for s in tried) else 0


def cmd_runs(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.run is None:
        if args.discard:
            parser.error("--discard needs RUN")
        base = args.workdir if args.workdir is not None else ap.runs_dir(args.db)
        for name, run in ap.list_runs(base):
            text = summary(run) if isinstance(run, ap.Run) else f"unreadable: {run}"
            print(f"{name}  {text}")
        return 0
    if args.discard:
        with hold(args, parser):
            run, root = load_run(args, parser)
            ap.discard(root)
        print(f"discarded run {run.name} ({summary(run)})")
        return 0
    run, root = load_run(args, parser)
    print(summary(run))
    return 0


# Parser


def filter_args(p: argparse.ArgumentParser, where: str = "") -> None:
    """Add ``--owner``, ``-s SET``, ``-t TOPIC`` (repeatable) and ``--any`` to *p*."""
    p.add_argument("--owner", help="only this owner's projects")
    p.add_argument(
        "-s", "--set", dest="set_name", metavar="SET", help="only projects in this set"
    )
    p.add_argument(
        "-t",
        "--topic",
        action="append",
        help=f"only projects with this topic{where}; repeat to need all of them",
    )
    p.add_argument("--any", action="store_true", help="need any -t topic, not all")


def positive(text: str) -> int:
    """Parse a positive int for argparse."""
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repodb",
        description="Keep a database of git project URLs; clone, compare and apply"
        " changes to projects in bulk.",
    )
    parser.add_argument(
        "--db", type=Path, default=DB_PATH, help=f"database file (default: {DB_PATH})"
    )
    # metavar replaces the {scan,github,...} list argparse prints twice.
    sub = parser.add_subparsers(
        dest="command", required=True, title="commands", metavar="COMMAND"
    )

    def add(name: str, handler: Handler, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.set_defaults(handler=handler)
        return p

    p = add("scan", cmd_scan, "add origin URLs of projects in DIR")
    p.add_argument(
        "directory",
        type=Path,
        nargs="?",
        default=SRC_DIR,
        metavar="DIR",
        help=f"default: {SRC_DIR}",
    )

    p = add("github", cmd_github, "add USER's GitHub repos (needs gh)")
    p.add_argument(
        "user",
        nargs="?",
        metavar="USER",
        help="GitHub user or organization; optional with --refresh",
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="update topics of stored github.com repos, all owners or USER's; add none",
    )
    p.add_argument(
        "--prune",
        action="store_true",
        help="with --refresh, list stored repos gh does not list",
    )
    p.add_argument("--yes", action="store_true", help="remove what --prune lists")
    p.add_argument(
        "-L",
        "--limit",
        type=int,
        default=10000,
        help="maximum repos to list (default: 10000)",
    )
    p.add_argument("--ssh", action="store_true", help="use SSH clone URLs, not HTTPS")
    p.add_argument("--source", action="store_true", help="omit forks")
    p.add_argument("--no-archived", action="store_true", help="omit archived repos")

    p = add("import", cmd_import, "add projects from a JSON file, flat or grouped")
    p.add_argument("json", type=Path, metavar="JSON")

    p = add("export", cmd_export, "write projects as {name: url} JSON")
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("projects.json"),
        help="output file, or - for stdout (default: projects.json)",
    )
    p.add_argument(
        "-g", "--group", action="store_true", help="write {owner: {name: url}}"
    )
    filter_args(p)

    p = add("list", cmd_list, "print project names")
    label = p.add_mutually_exclusive_group()
    label.add_argument("-u", "--urls", action="store_true", help="print URLs instead")
    label.add_argument(
        "--slugs",
        action="store_true",
        help="print OWNER/REPO from the url, e.g. for git-xargs or multi-gitter",
    )
    p.add_argument("-g", "--group", action="store_true", help="group by owner")
    filter_args(p)
    p.add_argument("--topics", action="store_true", help="show each project's topics")

    p = add("set", cmd_set, "add projects to a named set, or list sets")
    p.add_argument("--name", metavar="SET", help="the set to show or change")
    p.add_argument(
        "specs",
        nargs="*",
        metavar="SPEC",
        help="OWNER/NAME, or NAME if only one owner has it",
    )
    p.add_argument("--remove", action="store_true", help="remove SPEC ... from the set")
    p.add_argument("--delete", action="store_true", help="delete the set")

    p = add("topics", cmd_topics, "print each topic and its project count")
    p.add_argument("--owner", help="only this owner's projects")

    p = add("status", cmd_status, "compare DEST with the database")
    p.add_argument("dest", type=Path, metavar="DEST")
    p.add_argument("-g", "--group", action="store_true", help="DEST holds OWNER/NAME")
    filter_args(p)

    p = add(
        "remove",
        cmd_remove,
        "delete projects from the database; cloned directories are kept",
    )
    p.add_argument(
        "specs",
        nargs="*",
        metavar="SPEC",
        help="OWNER/NAME, or NAME if only one owner has it",
    )
    p.add_argument("--owner", help="remove every project of this owner; needs --all")
    p.add_argument("--all", action="store_true", help="confirm --owner")

    add("info", cmd_info, "describe the database; read-only")

    p = add("clone", cmd_clone, "clone projects into DEST")
    p.add_argument("dest", type=Path, metavar="DEST")
    source = p.add_mutually_exclusive_group()
    source.add_argument(
        "-r",
        "--repo",
        action="append",
        metavar="SPEC",
        help="only this project, as OWNER/NAME or NAME; repeatable",
    )
    filter_args(p, " (from the database or FILE)")
    source.add_argument(
        "--json",
        type=Path,
        metavar="FILE",
        help="clone from this JSON file instead of the database",
    )
    p.add_argument(
        "-g", "--group", action="store_true", help="clone into DEST/OWNER/NAME"
    )
    p.add_argument(
        "-j",
        "--jobs",
        type=positive,
        default=1,
        help="clones to run at once (default: 1)",
    )
    p.add_argument(
        "git_options",
        nargs="*",
        metavar="-- GIT_OPTION",
        help="options for git clone, after --",
    )

    def run_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("run", metavar="RUN", help="run name; also names the branch")
        p.add_argument(
            "--workdir",
            type=Path,
            help="directory holding runs (default: 'runs' next to the database)",
        )

    p = add("apply", cmd_apply, "prepare a change across repos as local commits")
    run_args(p)
    change = p.add_mutually_exclusive_group()
    change.add_argument(
        "--exec", metavar="CMD", help="shell command run in each repo root"
    )
    change.add_argument(
        "--replace",
        nargs=2,
        metavar=("PATTERN", "REPL"),
        help="Python regex substitution in files matching --glob",
    )
    p.add_argument("--glob", action="append", help="files for --replace; repeatable")
    p.add_argument("-m", "--message", help="commit message; first line is the PR title")
    p.add_argument(
        "-r", "--repo", action="append", metavar="SPEC", help="this project; repeatable"
    )
    filter_args(p)
    p.add_argument("--all", action="store_true", help="every project in the database")
    p.add_argument("--branch", help="branch name (default: repodb/RUN)")
    p.add_argument(
        "-j", "--jobs", type=positive, default=1, help="repos at once (default: 1)"
    )
    p.add_argument(
        "--timeout", type=positive, metavar="SECS", help="per-repo --exec limit"
    )
    p.add_argument(
        "--redo",
        nargs="*",
        metavar="SPEC",
        help="start these repos (default: all) over, with a changed change or message",
    )

    p = add("review", cmd_review, "show a run's per-repo state")
    run_args(p)
    p.add_argument("specs", nargs="*", metavar="SPEC", help="only these repos")
    p.add_argument("--diff", action="store_true", help="print each commit's diff")

    p = add("publish", cmd_publish, "push a run's commits and open PRs")
    run_args(p)
    p.add_argument("specs", nargs="*", metavar="SPEC", help="only these repos")
    p.add_argument(
        "--push-default", action="store_true", help="push to the default branch, no PR"
    )
    p.add_argument("--draft", action="store_true", help="open draft PRs")
    p.add_argument(
        "-j", "--jobs", type=positive, default=1, help="repos at once (default: 1)"
    )

    p = add("runs", cmd_runs, "list runs, or discard one")
    p.add_argument("run", nargs="?", metavar="RUN")
    p.add_argument("--discard", action="store_true", help="delete the run's directory")
    p.add_argument("--workdir", type=Path, help="directory holding runs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    args = parser.parse_args(argv)
    # nargs="*" also takes stray words given without "--"; keep them from git.
    if getattr(args, "git_options", None) and "--" not in argv:
        parser.error(f"unrecognized arguments: {' '.join(args.git_options)}")
    if getattr(args, "any", False) and not args.topic:
        parser.error("--any goes with -t")
    # Show library progress for this call only, and not twice via the root logger.
    log = logging.getLogger("repodb")
    console = Console()
    saved = log.level, log.propagate
    log.addHandler(console)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        handler: Handler = args.handler
        return handler(args, parser)
    finally:
        log.removeHandler(console)
        log.setLevel(saved[0])
        log.propagate = saved[1]
