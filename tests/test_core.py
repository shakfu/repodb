"""Tests for repodb.core: urls, the database, migration, scan, JSON files."""

import json
import logging
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from conftest import add_tagged, names, run, write_json
from repodb.core import (
    Change,
    GitRepoDB,
    flatten,
    host_of,
    info,
    owner_of,
    primary_key,
    read_projects,
    scan,
    url_key,
)


@pytest.mark.parametrize(
    "url, owner",
    [
        ("https://github.com/u/r.git", "u"),
        ("https://github.com/u/r", "u"),
        ("http://GitHub.com/u/r", "u"),
        ("git@github.com:u/r.git", "u"),
        ("ssh://git@github.com/u/r", "u"),
        ("ssh://git@ssh.github.com:443/u/r", "u"),
        ("git+ssh://git@github.com/u/r", "u"),
        ("https://gitlab.com/u/r.git", "u"),
        ("https://gitlab.com/group/sub/r.git", "group"),
        ("https://codeberg.org/u/r.git", "u"),
        ("https://github.com/u", None),
        ("/local/path/r.git", None),
        ("file:///local/path/r.git", None),
        ("./a/b", None),
        ("https://github.com/../r", None),
        ("git@github.com:./r", None),
    ],
)
def test_owner_of(url, owner):
    assert owner_of(url) == owner


def test_url_key():
    same = [
        "https://github.com/U/r.git",
        "http://github.com/u/r/",
        "git@github.com:u/R.git",
        "ssh://git@github.com:22/u/r",
    ]
    assert {url_key(u) for u in same} == {"github.com/u/r"}
    assert url_key("https://gitlab.com/g/a/r") != url_key("https://gitlab.com/g/b/r")
    assert url_key("/local/r.git") == "/local/r.git"


@pytest.mark.parametrize(
    "url, host",
    [
        ("https://GitHub.com/u/r", "github.com"),
        ("git@gitlab.com:u/r.git", "gitlab.com"),
        ("ssh://git@codeberg.org:22/u/r", "codeberg.org"),
        ("/local/r.git", None),
    ],
)
def test_host_of(url, host):
    assert host_of(url) == host


class TestInfo:
    def fields(self, pairs):
        return dict(pairs)

    def test_current(self, db_path):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    ("r", "https://github.com/alice/r"),
                    ("s", "git@github.com:Alice/s.git"),
                    ("r", "https://gitlab.com/bob/r"),
                    ("t", "https://github.com/carol/t"),
                ]
            )
            add_tagged(
                db,
                [
                    ("r", "https://github.com/alice/r", ["cli", "agent"]),
                    ("t", "https://github.com/carol/t", ["Agent"]),
                    ("s", "git@github.com:Alice/s.git", ["rag"]),
                ],
            )
        f = self.fields(info(db_path, top=2))
        assert f["database"].startswith(f"{db_path} (")
        assert f["format"] == "current, keyed by url"
        assert f["projects"] == "4"
        assert f["owners"] == "3"
        assert f["hosts"] == "github.com 3, gitlab.com 1"
        assert f["top owners"] == "alice 2, bob 1"
        assert f["shared names"] == "r (alice, bob)"
        assert f["topics"] == "3"
        assert f["top topics"] == "agent 2, cli 1"
        assert "no owner" not in f

    def test_without_topics_table(self, db_path):
        db_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(GitRepoDB.SCHEMA)
        f = self.fields(info(db_path))
        assert (f["topics"], f["top topics"]) == ("0", "-")
        with closing(sqlite3.connect(db_path)) as conn:  # info did not create it
            assert primary_key(conn) == GitRepoDB.KEY
            assert not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'topics'"
            ).fetchone()

    def test_unsupported_schema(self, db_path):
        old_db(db_path, [("r", "https://github.com/a/r", "a")])
        f = self.fields(info(db_path))
        assert f["format"] == "unsupported (primary key name)"
        assert list(f) == ["database", "format"]

    def test_empty_file(self, tmp_path):
        empty = tmp_path / "empty.sqlite"
        sqlite3.connect(empty).close()
        f = self.fields(info(empty))
        assert f["format"] == "no repos table"
        assert list(f) == ["database", "format"]

    def test_main(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add([("r", "https://github.com/a/r")])
        capsys.readouterr()
        assert run(db_path, "info") == 0
        out = capsys.readouterr().out.splitlines()
        assert (
            out[0] == f"database:     {db_path} ({db_path.stat().st_size / 1024:.1f}KB)"
        )
        # Every value starts in the same column.
        assert {len(line) - len(line.split(":", 1)[1].lstrip()) for line in out} == {14}

    @pytest.mark.parametrize(
        "args", [["list", "stray"], ["publish", "r", "--bogus"], ["set", "-x", "a"]]
    )
    def test_rejects_unrecognized_arguments(self, db_path, capsys, args):
        with pytest.raises(SystemExit) as e:
            run(db_path, *args)
        assert e.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "args",
        [
            ["list"],
            ["export", "-o", "-"],
            ["topics"],
            ["status", "."],
            ["set"],
            ["remove", "a/b"],
            ["clone", "d"],
            ["apply", "r", "--exec", "true", "-m", "m", "--all"],
        ],
    )
    def test_read_commands_need_a_database(self, db_path, capsys, args):
        with pytest.raises(SystemExit) as e:
            run(db_path, *args)
        assert f"no database at {db_path}" in str(e.value.code)
        assert not db_path.exists()

    def test_main_missing_does_not_create(self, db_path, capsys):
        assert run(db_path, "info") == 1
        assert f"no database at {db_path}" in capsys.readouterr().err
        assert not db_path.parent.exists()

    def test_main_not_sqlite(self, tmp_path, capsys):
        bad = tmp_path / "bad.sqlite"
        bad.write_text("not a database" * 100)
        assert run(bad, "info") == 1
        assert "bad.sqlite" in capsys.readouterr().err


class TestGitRepoDB:
    def test_creates_parent_dir(self, db_path):
        with GitRepoDB(db_path):
            pass
        assert db_path.is_file()

    def test_same_name_different_owners(self, db_path):
        with GitRepoDB(db_path) as db:
            db.add(
                [("r", "https://github.com/alice/r"), ("r", "git@gitlab.com:bob/r.git")]
            )
            assert db.rows() == [
                ("alice", "r", "https://github.com/alice/r"),
                ("bob", "r", "git@gitlab.com:bob/r.git"),
            ]

    def test_add_updates_changed_url(self, db_path):
        with GitRepoDB(db_path) as db:
            added = db.add([("r", "https://github.com/a/r")])
            assert db.add([("r", "https://github.com/a/r")]) == []
            # The key matches across url forms and case; the stored owner stays.
            updated = db.add([("r", "git@github.com:A/r.git")])
            assert db.rows() == [("a", "r", "git@github.com:A/r.git")]
        assert added == [Change("added", "a", "r", "https://github.com/a/r")]
        assert [str(c) for c in updated] == ["updated: a/r <- git@github.com:A/r.git"]

    def test_add_same_url_under_another_name_keeps_first(self, db_path):
        with GitRepoDB(db_path) as db:
            db.add([("mydir", "git@github.com:alice/real.git")])
            changes = db.add_projects(
                [("real", "https://github.com/alice/real", ["cli"])]
            )
            assert db.rows() == [("alice", "mydir", "https://github.com/alice/real")]
            assert [str(c) for c in changes] == [
                "updated: alice/mydir <- https://github.com/alice/real",
                "topics: alice/mydir <- cli",
            ]

    def test_add_rejects_taken_owner_name_atomically(self, db_path):
        with GitRepoDB(db_path) as db:
            db.add([("r", "https://github.com/a/r")])
            with pytest.raises(ValueError, match="a/r: held by https://github.com/a/r"):
                db.add(
                    [("x", "https://github.com/a/x"), ("r", "https://gitlab.com/a/r")]
                )
            assert db.rows() == [("a", "r", "https://github.com/a/r")]

    def test_add_rejects_ownerless_url_atomically(self, db_path):
        with GitRepoDB(db_path) as db:
            with pytest.raises(ValueError, match="no owner"):
                db.add([("ok", "https://github.com/a/ok"), ("bad", "/x/bad")])
            assert db.rows() == []

    @pytest.mark.parametrize("name", ["..", "../../victim", "a/b", ""])
    def test_add_rejects_unsafe_name_atomically(self, db_path, name):
        with GitRepoDB(db_path) as db:
            with pytest.raises(ValueError, match="not a valid project directory"):
                db.add(
                    [
                        ("ok", "https://github.com/a/ok"),
                        (name, "https://github.com/a/r"),
                    ]
                )
            assert db.rows() == []

    @pytest.mark.parametrize(
        "owner, name", [(None, "r"), ("", "r"), ("a", ""), ("a", None)]
    )
    def test_schema_rejects_empty_or_null(self, db_path, owner, name):
        with GitRepoDB(db_path) as db, pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO repos (owner, name, url) VALUES (?, ?, 'u')", (owner, name)
            )

    def test_rows_owner_filter_is_case_insensitive(self, db_path):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    ("z", "https://github.com/Alice/z"),
                    ("a", "https://github.com/alice/a"),
                    ("o", "https://github.com/bob/o"),
                ]
            )
            assert [n for _, n, _ in db.rows("ALICE")] == ["a", "z"]

    def test_by_owner(self, db_path):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    ("z", "https://github.com/bob/z"),
                    ("b", "git@github.com:Alice/b.git"),
                    ("a", "https://github.com/alice/a"),
                ]
            )
            # Mixed-case owners share a group, spelled as in its first project by name.
            assert db.by_owner() == {
                "alice": {
                    "a": "https://github.com/alice/a",
                    "b": "git@github.com:Alice/b.git",
                },
                "bob": {"z": "https://github.com/bob/z"},
            }

    def test_remove(self, db_path):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    ("r", "https://github.com/alice/r"),
                    ("r", "https://github.com/bob/r"),
                    ("s", "https://github.com/bob/s"),
                ]
            )
            assert db.remove(["ALICE/r", "s"]) == [
                ("alice", "r", "https://github.com/alice/r"),
                ("bob", "s", "https://github.com/bob/s"),
            ]
            assert db.rows() == [("bob", "r", "https://github.com/bob/r")]

    @pytest.mark.parametrize(
        "specs, message",
        [
            (["alice/s", "missing"], "'missing': no such project"),
            (["alice/s", "r"], "'r': ambiguous, use one of alice/r, bob/r"),
            (["carol/r"], "'carol/r': no such project"),
        ],
    )
    def test_remove_is_atomic(self, db_path, specs, message):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    ("r", "https://github.com/alice/r"),
                    ("r", "https://github.com/bob/r"),
                    ("s", "https://github.com/alice/s"),
                ]
            )
            with pytest.raises(ValueError) as e:
                db.remove(specs)
            assert str(e.value) == message
            assert len(db.rows()) == 3

    def test_find(self, db_path):
        with GitRepoDB(db_path) as db:
            db.add([("r", "https://github.com/a/r"), ("r", "https://github.com/b/r")])
            assert db.find("B/R") == ("b", "r", "https://github.com/b/r")
            with pytest.raises(ValueError, match="ambiguous, use one of a/r, b/r"):
                db.find("r")
            with pytest.raises(ValueError, match="'c/r': no such project"):
                db.find("c/r")

    def test_topics(self, db_path):
        with GitRepoDB(db_path) as db:
            add_tagged(
                db,
                [
                    ("r", "https://github.com/a/r", ["agent", "cli"]),
                    ("s", "https://github.com/a/s", ["cli"]),
                    ("t", "https://github.com/b/t", ["Agent"]),
                ],
            )
            db.add([("u", "https://github.com/b/u")])  # no topics
            assert names(db.rows(topics=["AGENT"])) == ["r", "t"]
            assert names(db.rows("b", ["agent"])) == ["t"]
            assert names(db.rows(topics=["cli"])) == ["r", "s"]
            # Adding without topics leaves stored topics alone.
            db.add([("r", "https://github.com/a/r")])
            assert len(db.rows(topics=["agent"])) == 2
            assert db.topics() == {
                ("a", "r"): ["agent", "cli"],
                ("a", "s"): ["cli"],
                ("b", "t"): ["Agent"],
            }

    def test_rows_all_or_any_topic(self, db_path):
        with GitRepoDB(db_path) as db:
            add_tagged(
                db,
                [
                    ("r", "https://github.com/a/r", ["agent", "cli"]),
                    ("s", "https://github.com/a/s", ["agent"]),
                    ("t", "https://github.com/a/t", ["CLI"]),
                ],
            )
            assert names(db.rows(topics=["agent", "Cli"])) == ["r"]
            assert names(db.rows(topics=["agent", "cli"], any_topic=True)) == [
                "r",
                "s",
                "t",
            ]
            assert names(db.rows(topics=["agent", "nope"])) == []
            with pytest.raises(TypeError):
                db.rows(topics="agent")

    def test_topics_keyed_by_name_and_url(self, db_path):
        # One name, two owners: each gets its own topics.
        with GitRepoDB(db_path) as db:
            add_tagged(
                db,
                [
                    ("r", "https://github.com/a/r", ["x"]),
                    ("r", "https://github.com/b/r", ["y"]),
                ],
            )
            assert db.topics() == {("a", "r"): ["x"], ("b", "r"): ["y"]}

    def test_add_reports_topic_changes(self, db_path):
        url = "https://github.com/a/r"
        with GitRepoDB(db_path) as db:
            changes = db.add_projects([("r", url, ["zed", "Agent", "agent"])])
            changes += db.add_projects([("r", url, ["AGENT", "Zed"])])  # same
            changes += db.add_projects([("r", url, ["cli"])])
            changes += db.add_projects([("r", url, [])])
            assert db.topics() == {}
        assert [str(c) for c in changes] == [
            f"added: a/r <- {url}",
            "topics: a/r <- Agent, zed",
            "topics: a/r <- cli",
            "topics: a/r <- -",
        ]

    def test_remove_drops_topics(self, db_path):
        with GitRepoDB(db_path) as db:
            add_tagged(db, [("r", "https://github.com/a/r", ["agent"])])
            assert db.rows(topics=["agent"])
            db.remove(["a/r"])
            db.add([("r", "https://github.com/a/r")])
            assert db.rows(topics=["agent"]) == []
            assert db.conn.execute("SELECT count(*) FROM topics").fetchone() == (0,)

    def test_opens_database_without_topics_table(self, db_path):
        db_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(GitRepoDB.SCHEMA)
            conn.execute(
                "INSERT INTO repos VALUES"
                " ('github.com/a/r', 'a', 'r', 'https://github.com/a/r')"
            )
        with GitRepoDB(db_path) as db:
            assert db.rows(topics=["agent"]) == []
            assert len(db.rows()) == 1

    def test_persists_across_connections(self, db_path):
        with GitRepoDB(db_path) as db:
            db.add([("r", "https://github.com/a/r")])
        with GitRepoDB(db_path) as db:
            assert db.rows() == [("a", "r", "https://github.com/a/r")]


def old_db(path, rows):
    """Create a table keyed by name alone, the pre-(owner, name) layout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE repos ("
        " name TEXT PRIMARY KEY, url TEXT NOT NULL, owner TEXT COLLATE NOCASE)"
    )
    conn.executemany("INSERT INTO repos VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


class TestUnsupportedSchema:
    def test_open_refuses_and_leaves_table(self, db_path):
        old_db(db_path, [("r", "https://github.com/a/r", "a")])
        with pytest.raises(ValueError, match="unsupported schema, primary key name$"):
            GitRepoDB(db_path)
        with closing(sqlite3.connect(db_path)) as conn:
            assert conn.execute("SELECT name, url FROM repos").fetchall() == [
                ("r", "https://github.com/a/r")
            ]

    def test_main_exits_1(self, db_path, capsys):
        old_db(db_path, [])
        with pytest.raises(SystemExit) as e:
            run(db_path, "list")
        assert "unsupported schema" in str(e.value.code)


def v01_db(path, rows, topics=(), sets=()):
    """Create a 0.1.x database keyed by (owner, name), with 0.2 dev topics and sets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE repos (owner TEXT NOT NULL COLLATE NOCASE,"
            " name TEXT NOT NULL COLLATE NOCASE, url TEXT NOT NULL,"
            " PRIMARY KEY (owner, name))"
        )
        conn.execute(
            "CREATE TABLE topics (owner TEXT COLLATE NOCASE, name TEXT COLLATE NOCASE,"
            " topic TEXT, PRIMARY KEY (owner, name, topic),"
            " FOREIGN KEY (owner, name) REFERENCES repos ON DELETE CASCADE)"
        )
        conn.execute(
            "CREATE TABLE sets (set_name TEXT, owner TEXT COLLATE NOCASE,"
            " name TEXT COLLATE NOCASE, PRIMARY KEY (set_name, owner, name),"
            " FOREIGN KEY (owner, name) REFERENCES repos ON DELETE CASCADE)"
        )
        conn.executemany("INSERT INTO repos VALUES (?, ?, ?)", rows)
        conn.executemany("INSERT INTO topics VALUES (?, ?, ?)", topics)
        conn.executemany("INSERT INTO sets VALUES (?, ?, ?)", sets)


class TestMigrate:
    ROWS = (
        ("alice", "mydir", "git@github.com:alice/real.git"),
        ("alice", "real", "https://github.com/alice/real"),
        ("bob", "s", "https://github.com/bob/s"),
    )

    def test_merges_same_url_and_moves_topics_and_sets(self, db_path, caplog):
        caplog.set_level(logging.INFO, logger="repodb")
        v01_db(
            db_path,
            self.ROWS,
            topics=[("alice", "mydir", "cli"), ("alice", "real", "agent")],
            sets=[("x", "alice", "real"), ("x", "bob", "s")],
        )
        with GitRepoDB(db_path) as db:
            assert db.rows() == [self.ROWS[0], self.ROWS[2]]
            assert db.topics() == {("alice", "mydir"): ["agent", "cli"]}
            assert names(db.rows(set_name="x")) == ["mydir", "s"]
            # Foreign keys point at the new table: removing cascades.
            db.remove(["mydir"])
            assert db.topics() == {}
            assert names(db.rows(set_name="x")) == ["s"]
        assert "merged alice/real into alice/mydir" in caplog.text
        with closing(sqlite3.connect(f"{db_path}.0.1.bak")) as bak:
            assert primary_key(bak) == GitRepoDB.OLD_KEY
            assert bak.execute("SELECT count(*) FROM repos").fetchone() == (3,)

    def test_cli_keeps_stdout_clean(self, db_path, capsys):
        v01_db(db_path, self.ROWS)
        assert run(db_path, "export", "-o", "-") == 0
        out, err = capsys.readouterr()
        assert json.loads(out) == {"mydir": self.ROWS[0][2], "s": self.ROWS[2][2]}
        assert "migrated" in err
        assert "merged alice/real into alice/mydir" in err

    def test_second_open_is_plain(self, db_path):
        v01_db(db_path, self.ROWS)
        GitRepoDB(db_path).close()
        Path(f"{db_path}.0.1.bak").unlink()
        with GitRepoDB(db_path) as db:
            assert len(db.rows()) == 2
        assert not Path(f"{db_path}.0.1.bak").exists()

    def test_refuses_to_overwrite_backup(self, db_path):
        v01_db(db_path, self.ROWS)
        Path(f"{db_path}.0.1.bak").write_text("keep")
        with pytest.raises(ValueError, match=r"cannot migrate, .*\.0\.1\.bak exists"):
            GitRepoDB(db_path)
        with closing(sqlite3.connect(db_path)) as conn:
            assert primary_key(conn) == GitRepoDB.OLD_KEY
        assert Path(f"{db_path}.0.1.bak").read_text() == "keep"

    def test_info_reports_pending_migration(self, db_path):
        v01_db(db_path, self.ROWS)
        f = dict(info(db_path))
        assert f["format"] == "0.1.x, keyed by (owner, name); migrated when next opened"
        with closing(sqlite3.connect(db_path)) as conn:
            assert primary_key(conn) == GitRepoDB.OLD_KEY


def test_scan_ignores_insteadof(src, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.git@github.com:.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    assert scan(src)[0] == ("a", "https://github.com/u/a.git")


def test_scan(src, caplog):
    assert scan(src) == [
        ("a", "https://github.com/u/a.git"),
        ("b", "git@gitlab.com:v/b.git"),
    ]
    assert caplog.messages == [
        "no owner in origin url, skipping: local (/some/path/local.git)",
        "no origin, skipping: noremote",
    ]
    assert {r.levelname for r in caplog.records} == {"WARNING"}


def test_flatten_rejects_repeated_name():
    with pytest.raises(ValueError, match="^'r': held by more than one owner$"):
        flatten([("r", "u1"), ("r", "u2")])


def test_flatten_ignores_case():
    # Foo and foo are one directory on a case-insensitive filesystem.
    with pytest.raises(
        ValueError, match="'foo': held by more than one owner \\(as 'Foo'\\)"
    ):
        flatten([("Foo", "u1"), ("foo", "u2")])
    assert flatten([("a", "u1"), ("b", "u2")]) == {"a": "u1", "b": "u2"}


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"a": 1},
        {"a": ""},
        {"../x": "u"},
        {"/abs": "u"},
        {"a/b": "u"},
        {"..": "u"},
        {"": "u"},
        {"o": {"a": 1}},
        {"o": {"../x": "u"}},
        {"o": {"a": "u"}, "b": "u"},
    ],
)
def test_read_projects_rejects_malformed(tmp_path, data):
    with pytest.raises(ValueError):
        read_projects(write_json(tmp_path / "p.json", data))


def test_read_projects_grouped_keeps_repeated_names(tmp_path):
    f = write_json(
        tmp_path / "p.json",
        {"x": {"r": "https://github.com/a/r"}, "y": {"r": "https://github.com/b/r"}},
    )
    assert read_projects(f) == [
        ("r", "https://github.com/a/r", None),
        ("r", "https://github.com/b/r", None),
    ]


@pytest.mark.parametrize(
    "data, expected",
    [
        (
            {"r": {"url": "u1", "topics": ["x"]}, "s": "u2"},
            [("r", "u1", ["x"]), ("s", "u2", None)],
        ),
        (
            {"o": {"r": {"url": "u1", "topics": []}}},
            [("r", "u1", [])],
        ),
        # A group holding repos named "url" and "topics" is not an entry.
        (
            {"o": {"url": "u1", "topics": "u2"}},
            [("url", "u1", None), ("topics", "u2", None)],
        ),
    ],
)
def test_read_projects_topics(tmp_path, data, expected):
    assert read_projects(write_json(tmp_path / "p.json", data)) == expected


@pytest.mark.parametrize(
    "data",
    [
        {"r": {"url": "u", "topics": [1]}},
        {"r": {"url": "u", "topics": [""]}},
        {"r": {"url": "", "topics": []}},
        {"r": {"url": "u", "topics": [], "extra": 1}, "s": "u"},
        {"o": {"r": {"url": "u"}}},
    ],
)
def test_read_projects_rejects_bad_entry(tmp_path, data):
    with pytest.raises(ValueError):
        read_projects(write_json(tmp_path / "p.json", data))


def test_has_no_runtime_dependencies():
    """The distribution must not declare runtime dependencies."""
    from importlib.metadata import requires

    assert not (requires("repodb") or [])


def test_imports_only_stdlib():
    """Importing repodb must not need any module outside the stdlib."""
    code = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        top = name.partition(".")[0]
        if top != "repodb" and top not in sys.stdlib_module_names:
            raise ImportError(f"non-stdlib import: {name}")
sys.meta_path.insert(0, Block())
import repodb.core
import repodb.apply
import repodb.cli
"""
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr
