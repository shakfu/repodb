"""A SQLite database of git project clone URLs, and operations on it.

Each row is ``(owner, name, url)``, keyed by the url as `url_key` gives it, so
one repo is one row whatever its url form. The owner is the first path
component after the host in the URL (``alice`` in ``github.com/alice/r`` or
``git@gitlab.com:alice/r``), and every row must have one, so local-path and
``file://`` remotes are not stored. The name is the clone directory. Owner and
name compare case-insensitively, and ``(owner, name)`` is unique too, so each
row has one ``OWNER/NAME`` directory.

Topics (from GitHub or JSON) and sets (local, named selections) are stored in
their own tables; `GitRepoDB.rows` filters by owner, set and topics.

The JSON form is ``{name: url}``, or grouped ``{owner: {name: url}}``. A
project with topics is written ``{"url": url, "topics": [...]}`` in place of
the url. Both forms are read back; group keys are ignored on read, since owner
is derived from the URL. The flat form rejects a name held by two owners.

Functions here return data and raise exceptions; progress and skipped items
are logged to the ``repodb`` logger. The command-line interface is
`repodb.cli`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypeVar

if TYPE_CHECKING:
    from typing_extensions import Self

log = logging.getLogger(__name__)

DB_PATH = Path("~/.local/share/repodb/repos.sqlite").expanduser()

# https://host/O/R, ssh://git@host:22/O/R, git@host:O/R.git; host needs a dot.
OWNER = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?([\w-]+(?:\.[\w-]+)+)(?::\d+)?[:/]([^/]+)/[^/]",
    re.IGNORECASE,
)

# As OWNER, but any host, and the whole path: GitLab subgroups nest.
URL = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?([^/:]+)(?::\d+)?[:/](.+)$",
    re.IGNORECASE,
)

Row = tuple[str, str, str]  # (owner, name, url)
Project = tuple[str, str, "list[str] | None"]  # (name, url, topics or None)
V = TypeVar("V")


def owner_of(url: str) -> str | None:
    """Return the owner in a ``host/owner/repo`` *url*, or None if it has none.

    An owner of ``.`` or ``..`` counts as none, since it is used as a directory.
    """
    m = OWNER.match(url)
    return m.group(2) if m and valid_name(m.group(2)) else None


def host_of(url: str) -> str | None:
    """Return the lower-cased host in a ``host/owner/repo`` *url*, or None."""
    m = OWNER.match(url)
    return m.group(1).lower() if m else None


def repo_of(url: str) -> str:
    """Return the last path component of *url*, without ``.git``."""
    return url.rstrip("/").rpartition("/")[2].removesuffix(".git")


def url_key(url: str) -> str:
    """Return *url* as lower-cased ``host/path``, without scheme, user, port or ``.git``.

    Https and ssh urls of one repo give the same key.
    """
    m = URL.match(url)
    if m is None:
        return url
    return f"{m[1]}/{m[2].rstrip('/').removesuffix('.git')}".lower()


def valid_name(name: str) -> bool:
    """True if *name* is a single path component, so ``DEST/name`` stays in DEST."""
    return name not in ("", ".", "..") and Path(name).name == name


def primary_key(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Return the primary-key columns of ``repos``, or () if there is no such table."""
    return tuple(r[1] for r in conn.execute("PRAGMA table_info(repos)") if r[5])


@dataclass(frozen=True)
class Change:
    """A row added or changed by `GitRepoDB.add`."""

    action: str  # "added", "updated" or "topics"
    owner: str
    name: str
    value: str  # the url, or the topics joined by ", " ("-" for none)

    def __str__(self) -> str:
        return f"{self.action}: {self.owner}/{self.name} <- {self.value}"


def info(db_path: Path, top: int = 5) -> list[tuple[str, str]]:
    """Describe the database at *db_path* as ``(field, value)`` pairs, read-only.

    Raises:
        sqlite3.Error: if the file cannot be opened as a database.
    """
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        pk = primary_key(conn)
        current = pk == GitRepoDB.KEY
        rows = (
            conn.execute("SELECT owner, name, url FROM repos").fetchall()
            if current
            else []
        )
        # Read-only, so a database made before topics may lack the table.
        has_topics = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'topics'"
        ).fetchone()
        topics = (
            conn.execute(
                "SELECT lower(topic) t, count(*) FROM topics GROUP BY t"
                " ORDER BY count(*) DESC, t"
            ).fetchall()
            if current and has_topics
            else []
        )
    finally:
        conn.close()
    fields = [
        ("database", f"{db_path} ({db_path.stat().st_size / 1024:.1f}KB)"),
        (
            "format",
            "current, keyed by url"
            if current
            else "0.1.x, keyed by (owner, name); migrated when next opened"
            if pk == GitRepoDB.OLD_KEY
            else f"unsupported (primary key {', '.join(pk)})"
            if pk
            else "no repos table",
        ),
    ]
    if current:
        owners: dict[str, list[str]] = {}  # lower-cased owner -> [spelling, names...]
        hosts: dict[str, int] = {}
        names: dict[str, list[str]] = {}  # lower-cased name -> owners
        for owner, name, url in rows:
            owners.setdefault(owner.lower(), [owner]).append(name)
            host = host_of(url) or "?"
            hosts[host] = hosts.get(host, 0) + 1
            names.setdefault(name.lower(), []).append(owner)

        def counts(items: Iterable[tuple[str, int]]) -> str:
            return ", ".join(f"{k} {n}" for k, n in items) or "-"

        by_size = sorted(owners.values(), key=lambda o: (-(len(o) - 1), o[0].lower()))
        shared = sorted((n, o) for n, o in names.items() if len(o) > 1)
        fields += [
            ("projects", str(len(rows))),
            ("owners", str(len(owners))),
            ("hosts", counts(sorted(hosts.items(), key=lambda h: (-h[1], h[0])))),
            ("top owners", counts((o[0], len(o) - 1) for o in by_size[:top])),
            ("topics", str(len(topics))),
            ("top topics", counts(topics[:top])),
            (
                "shared names",
                ", ".join(f"{n} ({', '.join(o)})" for n, o in shared) or "-",
            ),
        ]
    return fields


class GitRepoDB:
    """SQLite table of ``(owner, name, url)``, keyed by `url_key`."""

    KEY = ("url_key",)
    OLD_KEY = ("owner", "name")  # 0.1.x
    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS repos ("
        " url_key TEXT PRIMARY KEY CHECK (url_key <> ''),"
        " owner TEXT NOT NULL COLLATE NOCASE CHECK (owner <> ''),"
        " name TEXT NOT NULL COLLATE NOCASE CHECK (name <> ''),"
        " url TEXT NOT NULL CHECK (url <> ''),"
        " UNIQUE (owner, name))"
    )
    # Separate tables, so databases made before them need no migration.
    TOPICS_SCHEMA = (
        "CREATE TABLE IF NOT EXISTS topics ("
        " owner TEXT NOT NULL COLLATE NOCASE,"
        " name TEXT NOT NULL COLLATE NOCASE,"
        " topic TEXT NOT NULL COLLATE NOCASE CHECK (topic <> ''),"
        " PRIMARY KEY (owner, name, topic),"
        " FOREIGN KEY (owner, name) REFERENCES repos (owner, name) ON DELETE CASCADE)"
    )
    SETS_SCHEMA = (
        "CREATE TABLE IF NOT EXISTS sets ("
        " set_name TEXT NOT NULL COLLATE NOCASE CHECK (set_name <> ''),"
        " owner TEXT NOT NULL COLLATE NOCASE,"
        " name TEXT NOT NULL COLLATE NOCASE,"
        " PRIMARY KEY (set_name, owner, name),"
        " FOREIGN KEY (owner, name) REFERENCES repos (owner, name) ON DELETE CASCADE)"
    )

    def __init__(self, db_path: str | Path = DB_PATH):
        """Open or create the database, migrating a 0.1.x one; see `migrate`.

        Raises:
            ValueError: if an existing ``repos`` table has another primary key,
                or a 0.1.x database's backup path is taken.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        try:
            if primary_key(self.conn) == self.OLD_KEY:
                self.migrate()
        except BaseException:
            self.conn.close()
            raise
        self.conn.execute(self.SCHEMA)
        if (pk := primary_key(self.conn)) != self.KEY:
            self.conn.close()
            raise ValueError(
                f"{self.db_path}: unsupported schema, primary key {', '.join(pk)}"
            )
        self.conn.execute(self.TOPICS_SCHEMA)
        self.conn.execute(self.SETS_SCHEMA)
        self.conn.execute("PRAGMA foreign_keys = ON")  # per connection; off by default

    def migrate(self) -> None:
        """Rebuild an ``(owner, name)``-keyed database on `url_key`, in one transaction.

        The file is first copied to ``<db>.0.1.bak``. Rows sharing a url key
        merge into the first stored; their topics and sets move to it.

        Raises:
            ValueError: if the backup path exists; nothing is changed.
        """
        bak = self.db_path.with_name(self.db_path.name + ".0.1.bak")
        if bak.exists():
            raise ValueError(f"{self.db_path}: cannot migrate, {bak} exists; move it")
        with closing(sqlite3.connect(bak)) as b:
            self.conn.backup(b)
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master")}
        rows = self.conn.execute(
            "SELECT owner, name, url FROM repos ORDER BY rowid"
        ).fetchall()
        topics, sets = (
            self.conn.execute(sql).fetchall() if table in tables else []
            for table, sql in [
                ("topics", "SELECT owner, name, topic FROM topics"),
                ("sets", "SELECT owner, name, set_name FROM sets"),
            ]
        )
        kept: dict[str, Row] = {}
        into: dict[tuple[str, str], Row] = {}  # lower-cased (owner, name) -> kept
        for o, n, u in rows:
            k = kept.setdefault(url_key(u), (o, n, u))
            if k[:2] != (o, n):
                log.warning("merged %s/%s into %s/%s: same repo, %s", o, n, *k[:2], u)
            into[o.lower(), n.lower()] = k
        self.conn.execute("BEGIN")
        try:
            for t in ("topics", "sets", "repos"):
                self.conn.execute(f"DROP TABLE IF EXISTS {t}")
            for schema in (self.SCHEMA, self.TOPICS_SCHEMA, self.SETS_SCHEMA):
                self.conn.execute(schema)
            self.conn.executemany(
                "INSERT INTO repos (url_key, owner, name, url) VALUES (?, ?, ?, ?)",
                [(key, *row) for key, row in kept.items()],
            )
            for sql, extra in [
                ("INSERT OR IGNORE INTO topics (owner, name, topic)", topics),
                ("INSERT OR IGNORE INTO sets (owner, name, set_name)", sets),
            ]:
                self.conn.executemany(
                    sql + " VALUES (?, ?, ?)",
                    [
                        (*to[:2], x)
                        for o, n, x in extra
                        if (to := into.get((o.lower(), n.lower()))) is not None
                    ],
                )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        # A warning, so it goes to stderr and not into `export` or `list` output.
        log.warning("migrated %s to url keys; backup: %s", self.db_path, bak)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def add(
        self,
        projects: Iterable[tuple[str, str]],
        topics: Mapping[tuple[str, str], Iterable[str]] | None = None,
    ) -> list[Change]:
        """Insert or update ``(name, url)`` pairs; return what changed.

        A url whose `url_key` is stored updates that row's url and keeps its
        name. Replace the topics of each project whose ``(name, url)`` is a key
        of *topics*; leave the others' alone.

        Raises:
            ValueError: if a name is not a single path component, a url has no
                owner, or a new url's ``(owner, name)`` is held by another url;
                nothing is written.
        """
        rows = []
        for name, url in projects:
            if not valid_name(name):
                raise ValueError(f"{name!r}: not a valid project directory name")
            owner = owner_of(url)
            if owner is None:
                raise ValueError(f"{name!r}: no owner in url {url!r}")
            rows.append((owner, name, url))
        changes = []
        stored = []  # (owner, name) of each row as stored, and the topics key
        with self.conn:
            for owner, name, url in rows:
                key = url_key(url)
                row = self.conn.execute(
                    "SELECT owner, name, url FROM repos WHERE url_key = ?", (key,)
                ).fetchone()
                if row:
                    if row[2] != url:
                        changes.append(Change("updated", row[0], row[1], url))
                        self.conn.execute(
                            "UPDATE repos SET url = ? WHERE url_key = ?", (url, key)
                        )
                    stored.append((row[0], row[1], (name, url)))
                    continue
                taken = self.conn.execute(
                    "SELECT url FROM repos WHERE owner = ? AND name = ?", (owner, name)
                ).fetchone()
                if taken:
                    raise ValueError(
                        f"{owner}/{name}: held by {taken[0]}; remove it or add"
                        f" {url} under another name"
                    )
                changes.append(Change("added", owner, name, url))
                self.conn.execute(
                    "INSERT INTO repos (url_key, owner, name, url) VALUES (?, ?, ?, ?)",
                    (key, owner, name, url),
                )
                stored.append((owner, name, (name, url)))
            for owner, name, given_key in stored:
                if topics is None or (given := topics.get(given_key)) is None:
                    continue
                # Topics compare case-insensitively; keep the first spelling.
                first: dict[str, str] = {}
                for t in given:
                    first.setdefault(t.lower(), t)
                new = sorted(first.values(), key=str.lower)
                old = [
                    r[0]
                    for r in self.conn.execute(
                        "SELECT topic FROM topics WHERE owner = ? AND name = ?"
                        " ORDER BY topic",
                        (owner, name),
                    )
                ]
                if [t.lower() for t in old] == [t.lower() for t in new]:
                    continue
                changes.append(Change("topics", owner, name, ", ".join(new) or "-"))
                self.conn.execute(
                    "DELETE FROM topics WHERE owner = ? AND name = ?", (owner, name)
                )
                self.conn.executemany(
                    "INSERT INTO topics (owner, name, topic) VALUES (?, ?, ?)",
                    [(owner, name, t) for t in new],
                )
        return changes

    def add_projects(self, projects: Iterable[Project]) -> list[Change]:
        """Add ``(name, url, topics)``, replacing topics where they are not None."""
        projects = list(projects)
        return self.add(
            [(n, u) for n, u, _ in projects],
            {(n, u): t for n, u, t in projects if t is not None},
        )

    def find(self, spec: str) -> Row:
        """Return the row given as ``owner/name`` or a bare ``name``.

        Raises:
            ValueError: if *spec* matches no row, or a bare name matches several.
        """
        owner, _, name = spec.rpartition("/")
        sql = "SELECT owner, name, url FROM repos WHERE name = ?"
        if owner:
            matches = self.conn.execute(
                sql + " AND owner = ?", (name, owner)
            ).fetchall()
        else:
            matches = self.conn.execute(sql, (name,)).fetchall()
        if not matches:
            raise ValueError(f"{spec!r}: no such project")
        if len(matches) > 1:
            raise ValueError(
                f"{spec!r}: ambiguous, use one of "
                + ", ".join(f"{o}/{n}" for o, n, _ in matches)
            )
        row: Row = matches[0]
        return row

    def remove(self, specs: Iterable[str]) -> list[Row]:
        """Delete projects given as ``owner/name`` or a bare ``name``; return the rows.

        Raises:
            ValueError: if a spec matches no row, or a bare name matches several;
                nothing is deleted.
        """
        removed: list[Row] = []
        with self.conn:
            for spec in specs:
                row = self.find(spec)
                self.conn.execute(
                    "DELETE FROM repos WHERE owner = ? AND name = ?", row[:2]
                )
                removed.append(row)
        return removed

    def remove_owner(self, owner: str) -> list[Row]:
        """Delete every project of *owner*; return the rows.

        Raises:
            ValueError: if *owner* has no projects.
        """
        removed = self.rows(owner)
        if not removed:
            raise ValueError(f"{owner!r}: no projects for this owner")
        with self.conn:
            self.conn.execute("DELETE FROM repos WHERE owner = ?", (owner,))
        return removed

    def rows(
        self,
        owner: str | None = None,
        topics: Sequence[str] = (),
        any_topic: bool = False,
        set_name: str | None = None,
    ) -> list[Row]:
        """Return rows sorted by owner then name.

        Optionally keep only *owner*'s, only those in set *set_name*, and only
        those with every one of *topics*, or with *any_topic* at least one.
        """
        if isinstance(topics, str):  # a str is a Sequence[str] of characters
            raise TypeError("topics must be a sequence of topics, not a str")
        where, params = [], []
        if owner is not None:
            where.append("owner = ?")
            params.append(owner)
        if set_name is not None:
            where.append(
                "EXISTS (SELECT 1 FROM sets s WHERE s.owner = repos.owner"
                " AND s.name = repos.name AND s.set_name = ?)"
            )
            params.append(set_name)
        has = (
            "EXISTS (SELECT 1 FROM topics t WHERE t.owner = repos.owner"
            " AND t.name = repos.name AND t.topic IN ({}))"
        )
        if topics and any_topic:
            where.append(has.format(", ".join("?" * len(topics))))
            params += topics
        else:
            where += [has.format("?")] * len(topics)
            params += topics
        sql = "SELECT owner, name, url FROM repos"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self.conn.execute(sql + " ORDER BY owner, name", params).fetchall()

    def by_owner(
        self,
        owner: str | None = None,
        topics: Sequence[str] = (),
        any_topic: bool = False,
        set_name: str | None = None,
    ) -> dict[str, dict[str, str]]:
        """Return ``{owner: {name: url}}``, owners sorted, filtered as `rows`."""
        return group(self.rows(owner, topics, any_topic, set_name))

    def sets(self) -> list[tuple[str, int]]:
        """Return ``(set, projects)`` sorted by set name."""
        return self.conn.execute(
            "SELECT set_name, count(*) FROM sets GROUP BY set_name ORDER BY set_name"
        ).fetchall()

    def set_add(self, set_name: str, specs: Iterable[str]) -> list[Row]:
        """Add projects given as `find` specs to *set_name*; return those not
        already members.

        Raises:
            ValueError: if a spec matches no row, or several; nothing is added.
        """
        added: list[Row] = []
        with self.conn:
            # Keep an existing set's spelling, so one set has one name.
            row = self.conn.execute(
                "SELECT set_name FROM sets WHERE set_name = ? LIMIT 1", (set_name,)
            ).fetchone()
            spelling = row[0] if row else set_name
            for spec in specs:
                r = self.find(spec)
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO sets (set_name, owner, name) VALUES (?, ?, ?)",
                    (spelling, r[0], r[1]),
                )
                if cur.rowcount:
                    added.append(r)
        return added

    def set_remove(self, set_name: str, specs: Iterable[str]) -> list[Row]:
        """Remove projects given as `find` specs from *set_name*; return them.

        Raises:
            ValueError: if a spec matches no row, or one not in the set;
                nothing is removed.
        """
        removed: list[Row] = []
        with self.conn:
            for spec in specs:
                r = self.find(spec)
                if not self.conn.execute(
                    "DELETE FROM sets WHERE set_name = ? AND owner = ? AND name = ?",
                    (set_name, r[0], r[1]),
                ).rowcount:
                    raise ValueError(f"{spec!r}: not in set {set_name!r}")
                removed.append(r)
        return removed

    def set_delete(self, set_name: str) -> int:
        """Delete set *set_name*; return its size.

        Raises:
            ValueError: if there is no such set.
        """
        with self.conn:
            n = self.conn.execute(
                "DELETE FROM sets WHERE set_name = ?", (set_name,)
            ).rowcount
        if not n:
            raise ValueError(f"{set_name!r}: no such set")
        return n

    def topic_counts(self, owner: str | None = None) -> list[tuple[str, int]]:
        """Return ``(topic, projects)`` lower-cased, most used first."""
        sql = "SELECT lower(topic) t, count(*) FROM topics"
        params = [] if owner is None else [owner]
        if owner is not None:
            sql += " WHERE owner = ?"
        sql += " GROUP BY t ORDER BY count(*) DESC, t"
        return self.conn.execute(sql, params).fetchall()

    def topics(self) -> dict[tuple[str, str], list[str]]:
        """Return sorted topics keyed by lower-cased ``(owner, name)``."""
        tags: dict[tuple[str, str], list[str]] = {}
        for o, n, t in self.conn.execute(
            "SELECT owner, name, topic FROM topics ORDER BY topic"
        ):
            tags.setdefault((o.lower(), n.lower()), []).append(t)
        return tags


def group(rows: Iterable[Row]) -> dict[str, dict[str, str]]:
    """Return ``{owner: {name: url}}``, one group per owner regardless of case."""
    groups: dict[str, dict[str, str]] = {}
    spelling: dict[str, str] = {}  # lower-cased owner -> first spelling
    for o, name, url in rows:
        groups.setdefault(spelling.setdefault(o.lower(), o), {})[name] = url
    return groups


def flatten(pairs: Iterable[tuple[str, V]]) -> dict[str, V]:
    """Return ``{name: value}``.

    Raises:
        ValueError: if a name appears twice, ignoring case, since both would
            map to ``DEST/<name>`` on a case-insensitive filesystem.
    """
    projects: dict[str, V] = {}
    seen: dict[str, str] = {}  # lower-cased name -> first spelling
    for name, url in pairs:
        if (first := seen.get(name.lower())) is not None:
            raise ValueError(
                f"{name!r}: held by more than one owner"
                f"{'' if first == name else f' (as {first!r})'}"
            )
        seen[name.lower()] = name
        projects[name] = url
    return projects


def has_topics(
    tags: Iterable[str] | None, topics: Iterable[str], any_topic: bool = False
) -> bool:
    """True if *tags* holds every one of *topics*, or with *any_topic* one."""
    want = {t.lower() for t in topics}
    have = {t.lower() for t in tags or ()}
    return bool(want & have) if any_topic else want <= have


def origin_of(path: Path) -> str | None:
    """Return the ``origin`` url of the git project at *path*, or None."""
    # Not "remote get-url", which applies url.<base>.insteadOf rewrites, so a
    # local rewrite rule would change the stored or compared url.
    result = subprocess.run(
        ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def scan(directory: Path) -> list[tuple[str, str]]:
    """Return ``(name, origin_url)`` for each git project directly under *directory*.

    Projects without an ``origin`` remote, or whose url has no owner, are
    logged as warnings and omitted.
    """
    projects = []
    for p in sorted(directory.iterdir()):
        if not (p.is_dir() and (p / ".git").exists()):
            continue
        url = origin_of(p)
        if url is None:
            log.warning("no origin, skipping: %s", p.name)
        elif owner_of(url) is None:
            log.warning("no owner in origin url, skipping: %s (%s)", p.name, url)
        else:
            projects.append((p.name, url))
    return projects


def github(
    user: str,
    limit: int = 10000,
    ssh: bool = False,
    source: bool = False,
    no_archived: bool = False,
) -> list[tuple[str, str, tuple[str, ...]]]:
    """Return ``(name, clone_url, topics)`` for *user*'s GitHub repos via ``gh repo list``.

    Raises:
        subprocess.CalledProcessError: if ``gh`` fails, e.g. unknown user or no auth.
    """
    field = "sshUrl" if ssh else "url"
    cmd = ["gh", "repo", "list", user, "--limit", str(limit)]
    cmd += ["--json", f"name,{field},repositoryTopics"]
    if source:
        cmd.append("--source")
    if no_archived:
        cmd.append("--no-archived")
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    # gh gives null, not [], for a repo without topics.
    return sorted(
        (r["name"], r[field], tuple(t["name"] for t in r["repositoryTopics"] or ()))
        for r in json.loads(out)
    )


@dataclass
class Refresh:
    """What `refresh` found. It removes nothing; see `prunable`."""

    checked: int = 0  # github.com projects considered
    changes: list[Change] = field(default_factory=list)
    unlisted: list[Row] = field(default_factory=list)  # gh did not list them
    prunable: list[Row] = field(default_factory=list)  # unlisted, owner fully listed
    partial: dict[str, int] = field(default_factory=dict)  # owner -> repos listed
    failed: dict[str, str] = field(default_factory=dict)  # owner -> gh error


def refresh(db: GitRepoDB, owner: str | None = None, limit: int = 10000) -> Refresh:
    """Replace the topics of stored github.com projects, optionally only *owner*'s.

    Projects are matched by the repo name in their url, since `scan` stores
    the directory name. A project gh does not list is ``prunable`` only if its
    owner's listing is complete: an empty listing can mean lost access, and one
    of *limit* repos can be truncated.
    """
    rows = [r for r in db.rows(owner) if host_of(r[2]) == "github.com"]
    result = Refresh(checked=len(rows))
    listed: dict[str, dict[str, tuple[str, ...]]] = {}  # owner -> repo -> topics
    for o in sorted({o.lower() for o, _, _ in rows}):
        try:
            repos = github(o, limit)
        except subprocess.CalledProcessError as e:
            result.failed[o] = (e.stderr or "").strip()
            continue
        listed[o] = {n.lower(): t for n, _, t in repos}
        if not repos or len(repos) >= limit:
            result.partial[o] = len(repos)
    topics = {}
    for o, n, u in rows:
        if (repos_of := listed.get(o.lower())) is None:
            continue
        if (t := repos_of.get(repo_of(u).lower())) is not None:
            topics[n, u] = t
        else:
            result.unlisted.append((o, n, u))
            if o.lower() not in result.partial:
                result.prunable.append((o, n, u))
    result.changes = db.add(list(topics), topics)
    return result


def is_entry(value: object) -> bool:
    """True if *value* is ``{"url": ..., "topics": [...]}``.

    A group's values are never lists, so this does not match a group.
    """
    return (
        isinstance(value, dict)
        and value.keys() == {"url", "topics"}
        and isinstance(value["topics"], list)
    )


def read_projects(path: Path) -> list[Project]:
    """Load and validate a flat or grouped projects file as ``(name, url, topics)``.

    Each value is a url, or ``{"url": url, "topics": [...]}``; topics is None
    for a bare url. A name may repeat across groups; `flatten` rejects that.

    Raises:
        OSError: if the file cannot be read.
        ValueError: if the file is neither ``{name: value}`` nor
            ``{owner: {name: value}}``, a name is not a single path component,
            or topics are not non-empty strings.
    """
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(  # noqa: TRY004 - bad file content; callers catch ValueError
            "expected a JSON object of {name: url} or {owner: {name: url}}"
        )
    if data and all(isinstance(v, dict) and not is_entry(v) for v in data.values()):
        items = [item for group in data.values() for item in group.items()]
    else:
        items = list(data.items())
    projects: list[Project] = []
    for name, value in items:
        url, topics = (
            (value["url"], value["topics"]) if is_entry(value) else (value, None)
        )
        if not isinstance(url, str) or not url:
            raise ValueError(
                f"{name!r}: expected a url or {{url, topics}}, got {value!r}"
            )
        if not valid_name(name):
            raise ValueError(f"{name!r}: not a valid project directory name")
        if topics is not None and not all(isinstance(t, str) and t for t in topics):
            raise ValueError(f"{name!r}: topics must be non-empty strings")
        projects.append((name, url, topics))
    return projects


def import_projects(db: GitRepoDB, path: Path) -> list[Change]:
    """Add the projects in JSON file *path* to *db*; return what changed.

    Raises:
        OSError, ValueError: as `read_projects` and `GitRepoDB.add`.
    """
    return db.add_projects(read_projects(path))


def export_projects(
    db: GitRepoDB,
    grouped: bool = False,
    owner: str | None = None,
    topics: Sequence[str] = (),
    any_topic: bool = False,
    set_name: str | None = None,
) -> dict[str, object]:
    """Return projects as ``{name: value}``, or *grouped* ``{owner: {name: value}}``.

    A value is the url, or ``{"url": url, "topics": [...]}`` for a project
    with topics. Filters are as `GitRepoDB.rows`.

    Raises:
        ValueError: if not *grouped* and a name is held by two owners.
    """
    tags = db.topics()

    def entry(o: str, n: str, u: str) -> str | dict[str, object]:
        t = tags.get((o.lower(), n.lower()))
        return {"url": u, "topics": t} if t else u

    rows = db.rows(owner, topics, any_topic, set_name)
    if grouped:
        return {
            o: {n: entry(o, n, u) for n, u in g.items()} for o, g in group(rows).items()
        }
    return dict(flatten((n, entry(o, n, u)) for o, n, u in rows))


def to_json(projects: Mapping[str, object]) -> str:
    """Return *projects* as the JSON text `export` writes."""
    return json.dumps(projects, indent=2) + "\n"


def clone(
    rows: Iterable[tuple[str | None, str, str]],
    dest: Path,
    by_owner: bool = False,
    jobs: int = 1,
    options: Sequence[str] = (),
    live: bool = False,
) -> list[str]:
    """Clone each ``(owner, name, url)`` into ``dest/<name>`` unless it exists.

    With *by_owner*, clone into ``dest/<owner>/<name>``. *jobs* clones run at
    once. *options* go to ``git clone``. With *live* and one job, git writes
    its progress to this process's stdout and stderr; otherwise its output is
    captured and a failure's is logged. A row whose url `url_key` matches an
    earlier row's is skipped. Return the failed targets.

    Raises:
        ValueError: if a target is not a path inside *dest*, or with
            *by_owner* a row has no owner; nothing is cloned.
    """
    rows = list(rows)
    # The database can be edited outside repodb, so check every component
    # stays inside DEST before cloning anything.
    if by_owner and (ownerless := [n for o, n, _ in rows if o is None]):
        raise ValueError(f"no owner in url: {', '.join(map(repr, ownerless))}")
    bad = [
        f"{o}/{n}" if by_owner else n
        for o, n, _ in rows
        if not valid_name(n) or (by_owner and not valid_name(o or ""))
    ]
    if bad:
        raise ValueError(f"invalid owner or name: {', '.join(map(repr, bad))}")
    dest.mkdir(parents=True, exist_ok=True)
    failed = []
    todo: list[tuple[Path, str]] = []
    seen: dict[str, str] = {}
    for owner, name, url in rows:
        if (first := seen.setdefault(url_key(url), name)) != name:
            log.warning("same repo as %s, skipping: %s (%s)", first, name, url)
            continue
        rel = Path(owner, name) if by_owner and owner else Path(name)
        if (dest / rel).exists():
            log.info("exists, skipping: %s", rel)
            continue
        todo.append((rel, url))
    stream = live and jobs == 1

    def run(rel: Path, url: str) -> bool:
        # "--" stops a url beginning with "-" being read as a git option.
        cmd = ["git", "clone", *options, "--", url, str(dest / rel)]
        if stream:
            log.info("cloning: %s <- %s", rel, url)
            return subprocess.run(cmd, check=False).returncode == 0
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if r.returncode == 0:
            log.info("cloned: %s <- %s", rel, url)
        else:
            log.warning("clone failed: %s <- %s\n%s", rel, url, r.stderr.rstrip())
        return r.returncode == 0

    if jobs == 1:
        results = [run(*t) for t in todo]
    else:
        with ThreadPoolExecutor(jobs) as pool:
            results = list(pool.map(lambda t: run(*t), todo))
    failed += [str(rel) for (rel, _), ok in zip(todo, results) if not ok]
    return failed


class Difference(NamedTuple):
    """One way a clone directory differs from the database."""

    kind: str  # missing, not a git repo, no origin, differs, untracked
    path: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.kind}: {self.path}" + (
            f" ({self.detail})" if self.detail else ""
        )


def status(
    rows: Iterable[Row],
    dest: Path,
    by_owner: bool = False,
    known: Iterable[Row] | None = None,
) -> list[Difference]:
    """Compare clones in *dest* with *rows*; return the differences.

    Directories are untracked if no row of *known* (default: *rows*) maps to
    them, so a filtered *rows* need not report other projects' clones.
    Dot-directories are skipped.
    """
    rows = list(rows)
    known = rows if known is None else list(known)

    def rel(owner: str, name: str) -> Path:
        return Path(owner, name) if by_owner else Path(name)

    found = []
    for owner, name, url in rows:
        r = rel(owner, name)
        target = dest / r
        if not target.exists():
            found.append(Difference("missing", str(r)))
        elif not (target / ".git").exists():
            found.append(Difference("not a git repo", str(r)))
        elif (origin := origin_of(target)) is None:
            found.append(Difference("no origin", str(r)))
        elif origin != url:
            found.append(
                Difference("differs", str(r), f"origin {origin}, stored {url}")
            )
    # Lower-cased: the database compares names without case, as macOS paths do.
    paths = {str(rel(o, n)).lower() for o, n, _ in known}
    owners = {o.lower() for o, _, _ in known}

    def dirs(path: Path) -> list[Path]:
        return sorted(
            p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")
        )

    if dest.is_dir():
        for d in dirs(dest):
            if not by_owner:
                if d.name.lower() not in paths:
                    found.append(Difference("untracked", d.name))
            elif d.name.lower() not in owners:
                found.append(Difference("untracked", d.name))
            else:
                for sub in dirs(d):
                    r = Path(d.name, sub.name)
                    if str(r).lower() not in paths:
                        found.append(Difference("untracked", str(r)))
    return found
