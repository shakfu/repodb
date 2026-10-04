"""Tests for repodb.py"""

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from conftest import HOST, git, run
from repodb.core import (
    Change,
    GitRepoDB,
    clone,
    flatten,
    github,
    host_of,
    info,
    owner_of,
    primary_key,
    read_projects,
    scan,
    url_key,
)


@pytest.fixture
def src(tmp_path):
    """A projects dir: repos with owned origins, one local origin, one without, a plain dir."""
    root = tmp_path / "src"
    for name, url in [
        ("a", "https://github.com/u/a.git"),
        ("b", "git@gitlab.com:v/b.git"),
        ("local", "/some/path/local.git"),
    ]:
        (root / name).mkdir(parents=True)
        git("-C", str(root / name), "init", "-q")
        git("-C", str(root / name), "remote", "add", "origin", url)
    (root / "noremote").mkdir()
    git("-C", str(root / "noremote"), "init", "-q")
    (root / "plain").mkdir()
    return root


def add_tagged(db, items):
    """Add ``(name, url, topics)`` items, replacing their topics."""
    db.add([(n, u) for n, u, _ in items], {(n, u): t for n, u, t in items})


def names(rows):
    return [n for _, n, _ in rows]


def write_json(path, data):
    path.write_text(json.dumps(data))
    return path


def cloned_targets(sp):
    return [c.args[0][-1] for c in sp.call_args_list]


@pytest.fixture
def fake_git():
    with patch("repodb.core.subprocess.run") as sp:
        sp.return_value = subprocess.CompletedProcess([], 0)
        yield sp


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


class TestClone:
    def test_flat_skips_existing(self, remotes, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="repodb")
        dest = tmp_path / "dest"
        (dest / "alpha").mkdir(parents=True)
        rows = [
            ("alice", "alpha", remotes["alice", "alpha"]),
            ("alice", "beta", remotes["alice", "beta"]),
        ]
        assert clone(rows, dest) == []
        assert not (dest / "alpha" / "README").exists()
        assert (dest / "beta" / "README").read_text() == "alice/beta"
        assert "exists, skipping: alpha" in caplog.messages

    def test_by_owner(self, remotes, tmp_path):
        dest = tmp_path / "dest"
        rows = [(o, n, u) for (o, n), u in remotes.items()]
        assert clone(rows, dest, by_owner=True) == []
        assert (dest / "alice" / "alpha" / "README").read_text() == "alice/alpha"
        assert (dest / "bob" / "alpha" / "README").read_text() == "bob/alpha"

    def test_rejects_unsafe_name(self, tmp_path, fake_git):
        rows = [("a", "ok", "u1"), ("a", "../escape", "u2")]
        with pytest.raises(ValueError, match="invalid owner or name: '../escape'"):
            clone(rows, tmp_path / "d")
        fake_git.assert_not_called()
        assert not (tmp_path / "d").exists()

    @pytest.mark.parametrize(
        "owner, message",
        [("..", "invalid owner or name: '../r'"), (None, "no owner in url: 'r'")],
    )
    def test_by_owner_rejects_unsafe_or_missing_owner(
        self, tmp_path, fake_git, owner, message
    ):
        # owner_of never returns "..", but the database can be edited by hand.
        rows = [("a", "ok", "u1"), (owner, "r", "u2")]
        with pytest.raises(ValueError, match=re.escape(message)):
            clone(rows, tmp_path, by_owner=True)
        fake_git.assert_not_called()

    def test_skips_same_repo_under_two_names(self, tmp_path, fake_git, caplog):
        rows = [
            ("a", "mydir", "git@github.com:a/real.git"),
            ("a", "real", "https://github.com/a/real"),
        ]
        caplog.set_level(logging.WARNING, logger="repodb")
        assert clone(rows, tmp_path) == []
        assert fake_git.call_count == 1
        assert "same repo as mydir, skipping: real" in caplog.text

    def test_reports_git_failure(self, remotes, tmp_path):
        rows = [
            ("alice", "alpha", remotes["alice", "alpha"]),
            ("alice", "gone", f"{HOST}alice/gone.git"),
        ]
        assert clone(rows, tmp_path / "dest") == ["gone"]
        assert (tmp_path / "dest" / "alpha" / "README").exists()


TAGGED: dict[str, dict[str, object]] = {
    "alice": {
        "a": {"url": "https://github.com/alice/a", "topics": ["agent", "cli"]},
        "r": "https://github.com/alice/r",
    },
    "bob": {"r": {"url": "git@github.com:bob/r.git", "topics": ["rag"]}},
}


class TestMain:
    def test_scan_export_roundtrip(self, src, db_path, tmp_path):
        out = tmp_path / "p.json"
        assert run(db_path, "scan", src) == 0
        assert run(db_path, "export", "-o", out) == 0
        assert json.loads(out.read_text()) == {
            "a": "https://github.com/u/a.git",
            "b": "git@gitlab.com:v/b.git",
        }

    def test_scan_rejects_non_directory(self, db_path, tmp_path):
        with pytest.raises(SystemExit):
            run(db_path, "scan", tmp_path / "missing")

    def test_export_unwritable(self, src, db_path, tmp_path, capsys):
        run(db_path, "scan", src)
        assert run(db_path, "export", "-o", tmp_path / "missing" / "p.json") == 1
        err = capsys.readouterr().err
        assert "cannot write" in err and "No such file or directory" in err

    def test_export_owner_filter(self, src, db_path, tmp_path):
        out = tmp_path / "p.json"
        run(db_path, "scan", src)
        assert run(db_path, "export", "--owner", "V", "-o", out) == 0
        assert json.loads(out.read_text()) == {"b": "git@gitlab.com:v/b.git"}

    def test_export_flat_rejects_repeated_name(
        self, remotes, db_path, tmp_path, capsys
    ):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
        with pytest.raises(SystemExit):
            run(db_path, "export", "-o", tmp_path / "p.json")
        assert (
            "'alpha': held by more than one owner; use --group"
            in capsys.readouterr().err
        )
        assert not (tmp_path / "p.json").exists()

    def test_export_grouped_roundtrip(self, remotes, db_path, tmp_path):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
        out = tmp_path / "p.json"
        assert run(db_path, "export", "-g", "-o", out) == 0
        assert json.loads(out.read_text()) == {
            "alice": {
                "alpha": remotes["alice", "alpha"],
                "beta": remotes["alice", "beta"],
            },
            "bob": {"alpha": remotes["bob", "alpha"]},
        }
        other = tmp_path / "other.sqlite"
        assert run(other, "import", out) == 0
        with GitRepoDB(db_path) as a, GitRepoDB(other) as b:
            assert a.rows() == b.rows()

    # Mixed hosts, URL forms, and owner case. Names are unique so the flat form holds them.
    ROUNDTRIP = (
        ("b", "git@github.com:Alice/b.git"),
        ("a", "https://github.com/alice/a"),
        ("t", "https://gitlab.com/jobol/t.git"),
        ("v", "ssh://git@codeberg.org:22/uzu/v.git"),
        ("z", "https://github.com/bob/z"),
    )

    @pytest.mark.parametrize("flags", [[], ["-g"]])
    def test_export_import_export_roundtrip(self, db_path, tmp_path, flags):
        with GitRepoDB(db_path) as db:
            db.add(self.ROUNDTRIP)
        first, second = tmp_path / "1.json", tmp_path / "2.json"
        other = tmp_path / "other.sqlite"
        assert run(db_path, "export", *flags, "-o", first) == 0
        assert run(other, "import", first) == 0
        assert run(other, "export", *flags, "-o", second) == 0
        with GitRepoDB(db_path) as a, GitRepoDB(other) as b:
            assert a.rows() == b.rows()
            assert len(b.rows()) == len(self.ROUNDTRIP)
        assert second.read_bytes() == first.read_bytes()

    @pytest.mark.parametrize(
        "data",
        [
            {"a": "https://github.com/alice/a", "t": "https://gitlab.com/jobol/t.git"},
            {
                "alice": {
                    "a": "https://github.com/alice/a",
                    "r": "https://github.com/alice/r",
                },
                "bob": {"r": "git@github.com:bob/r.git"},
            },
        ],
    )
    def test_import_export_reproduces_canonical_json(self, db_path, tmp_path, data):
        # Canonical: sorted keys, group keys equal to the URL owner.
        grouped = isinstance(next(iter(data.values())), dict)
        src, out = tmp_path / "in.json", tmp_path / "out.json"
        src.write_text(json.dumps(data, indent=2) + "\n")
        assert run(db_path, "import", src) == 0
        assert run(db_path, "export", *(["-g"] if grouped else []), "-o", out) == 0
        assert out.read_text() == src.read_text()

    def test_import_ownerless_prints_nothing(self, db_path, tmp_path, capsys):
        f = write_json(
            tmp_path / "p.json", {"ok": "https://github.com/a/ok", "bad": "/x/bad"}
        )
        with pytest.raises(SystemExit):
            run(db_path, "import", f)
        out = capsys.readouterr()
        assert out.out == ""
        assert "'bad': no owner" in out.err

    def test_clone_flat_rejects_names_differing_in_case(
        self, db_path, tmp_path, fake_git
    ):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    ("Foo", "https://github.com/alice/Foo"),
                    ("foo", "https://github.com/bob/foo"),
                ]
            )
        with pytest.raises(SystemExit):
            run(db_path, "clone", tmp_path / "d")
        fake_git.assert_not_called()

    def test_import_then_list(self, db_path, tmp_path, capsys):
        f = write_json(
            tmp_path / "p.json",
            {"r": "https://github.com/a/r", "s": "https://gitlab.com/b/s"},
        )
        assert run(db_path, "import", f) == 0
        capsys.readouterr()
        assert run(db_path, "list", "--urls", "--owner", "a") == 0
        assert capsys.readouterr().out == "https://github.com/a/r\n"

    @pytest.mark.parametrize("content", ["{not json", '{"r": "/x/local"}'])
    def test_import_rejects_malformed_or_ownerless(self, db_path, tmp_path, content):
        f = tmp_path / "p.json"
        f.write_text(content)
        with pytest.raises(SystemExit):
            run(db_path, "import", f)
        with GitRepoDB(db_path) as db:
            assert db.rows() == []

    def test_list_flat_and_grouped(self, remotes, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
        capsys.readouterr()
        assert run(db_path, "list") == 0
        assert capsys.readouterr().out == "alpha\nbeta\nalpha\n"
        assert run(db_path, "list", "-g") == 0
        assert capsys.readouterr().out == "alice\n  alpha\n  beta\nbob\n  alpha\n"
        assert run(db_path, "list", "-g", "-u", "--owner", "BOB") == 0
        assert capsys.readouterr().out == f"bob\n  {remotes['bob', 'alpha']}\n"

    @pytest.mark.parametrize("flags", [[], ["-g"]])
    def test_list_empty(self, db_path, capsys, flags):
        GitRepoDB(db_path).close()
        assert run(db_path, "list", *flags) == 1
        assert "no projects" in capsys.readouterr().err

    def test_list_topic(self, remotes, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
            add_tagged(
                db,
                [
                    ("alpha", remotes["bob", "alpha"], ["agent"]),
                    ("beta", remotes["alice", "beta"], ["agent"]),
                ],
            )
        capsys.readouterr()
        assert run(db_path, "list", "-t", "agent") == 0
        assert capsys.readouterr().out == "beta\nalpha\n"
        assert run(db_path, "list", "-t", "agent", "-g", "--owner", "bob") == 0
        assert capsys.readouterr().out == "bob\n  alpha\n"
        assert run(db_path, "list", "-t", "nope") == 1
        assert "no projects with topic 'nope'" in capsys.readouterr().err

    def test_import_export_topics_grouped(self, db_path, tmp_path):
        src, out = tmp_path / "in.json", tmp_path / "out.json"
        src.write_text(json.dumps(TAGGED, indent=2) + "\n")
        assert run(db_path, "import", src) == 0
        assert run(db_path, "export", "-g", "-o", out) == 0
        assert out.read_text() == src.read_text()
        with GitRepoDB(db_path) as db:
            assert names(db.rows(topics=["rag"])) == ["r"]

    def test_import_export_topics_flat(self, db_path, tmp_path):
        data = {"a": TAGGED["alice"]["a"], "r": "https://github.com/alice/r"}
        src, out = tmp_path / "in.json", tmp_path / "out.json"
        src.write_text(json.dumps(data, indent=2) + "\n")
        assert run(db_path, "import", src) == 0
        assert run(db_path, "export", "-o", out) == 0
        assert out.read_text() == src.read_text()

    @pytest.mark.parametrize("flags", [[], ["-g"]])
    def test_export_topic(self, db_path, tmp_path, flags):
        src, out = tmp_path / "in.json", tmp_path / "out.json"
        write_json(src, TAGGED)
        assert run(db_path, "import", src) == 0
        assert run(db_path, "export", "-t", "AGENT", *flags, "-o", out) == 0
        a = TAGGED["alice"]["a"]
        assert json.loads(out.read_text()) == (
            {"alice": {"a": a}} if flags else {"a": a}
        )
        # A topic held by nothing exits 1 and writes no file.
        out.unlink()
        assert run(db_path, "export", "-t", "nope", *flags, "-o", out) == 1
        assert not out.exists()

    def test_clone_json_topic(self, db_path, tmp_path, fake_git, capsys):
        f = write_json(tmp_path / "p.json", TAGGED)
        d = tmp_path / "d"
        assert run(db_path, "clone", "--json", f, "-t", "Rag", d) == 0
        assert cloned_targets(fake_git) == [str(d / "r")]
        assert not db_path.exists()
        assert run(db_path, "clone", "--json", f, "-t", "nope", d) == 1
        assert f"no projects with topic 'nope' in {f}" in capsys.readouterr().err
        assert len(fake_git.call_args_list) == 1

    def test_list_several_topics(self, db_path, tmp_path, capsys):
        assert run(db_path, "import", write_json(tmp_path / "p.json", TAGGED)) == 0
        capsys.readouterr()
        assert run(db_path, "list", "-t", "agent", "-t", "cli") == 0
        assert capsys.readouterr().out == "a\n"
        assert run(db_path, "list", "-t", "agent", "-t", "rag", "--any") == 0
        assert capsys.readouterr().out == "a\nr\n"
        assert run(db_path, "list", "-t", "agent", "-t", "rag") == 1
        assert "no projects with topics 'agent' and 'rag'" in capsys.readouterr().err
        with pytest.raises(SystemExit):
            run(db_path, "list", "--any")

    def test_list_slugs(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    ("mydir", "git@github.com:alice/real.git"),
                    ("real", "https://github.com/alice/real"),  # same repo
                    ("t", "https://gitlab.com/bob/t.git"),
                ]
            )
        capsys.readouterr()
        assert run(db_path, "list", "--slugs") == 0
        assert capsys.readouterr().out == "alice/real\nbob/t\n"
        assert run(db_path, "list", "--slugs", "-g", "--owner", "bob") == 0
        assert capsys.readouterr().out == "bob\n  bob/t\n"
        with pytest.raises(SystemExit):
            run(db_path, "list", "--slugs", "-u")

    def test_clone_json_several_topics(self, db_path, tmp_path, fake_git, capsys):
        f = write_json(tmp_path / "p.json", TAGGED)
        d = tmp_path / "d"
        assert (
            run(db_path, "clone", "-g", "--json", f, "-t", "agent", "-t", "CLI", d) == 0
        )
        assert cloned_targets(fake_git) == [str(d / "alice" / "a")]
        fake_git.reset_mock()
        args = ["clone", "-g", "--json", f, "-t", "cli", "-t", "rag", "--any", d]
        assert run(db_path, *args) == 0
        assert cloned_targets(fake_git) == [
            str(d / "alice" / "a"),
            str(d / "bob" / "r"),
        ]
        assert run(db_path, "clone", "--json", f, "-t", "cli", "-t", "rag", d) == 1
        assert f"topics 'cli' and 'rag' in {f}" in capsys.readouterr().err

    def test_clone_git_options(self, db_path, tmp_path, fake_git):
        with GitRepoDB(db_path) as db:
            db.add([("r", "https://github.com/a/r")])
        d = tmp_path / "d"
        assert run(db_path, "clone", d, "--", "--depth", "1", "-b", "main") == 0
        assert fake_git.call_args.args[0] == [
            "git",
            "clone",
            "--depth",
            "1",
            "-b",
            "main",
            "--",
            "https://github.com/a/r",
            str(d / "r"),
        ]

    def test_clone_rejects_stray_argument(self, db_path, tmp_path, fake_git):
        with pytest.raises(SystemExit):
            run(db_path, "clone", tmp_path / "d", "--depth=1")
        with pytest.raises(SystemExit):
            run(db_path, "clone", tmp_path / "d", "stray")
        fake_git.assert_not_called()

    def test_import_bare_url_keeps_topics(self, db_path, tmp_path):
        url = "https://github.com/a/r"
        with GitRepoDB(db_path) as db:
            add_tagged(db, [("r", url, ["agent"])])
        assert run(db_path, "import", write_json(tmp_path / "1.json", {"r": url})) == 0
        with GitRepoDB(db_path) as db:
            assert db.topics() == {("a", "r"): ["agent"]}
        cleared = {"r": {"url": url, "topics": []}}
        assert run(db_path, "import", write_json(tmp_path / "2.json", cleared)) == 0
        with GitRepoDB(db_path) as db:
            assert db.topics() == {}

    def test_remove(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add([("r", "https://github.com/a/r"), ("s", "https://github.com/a/s")])
        capsys.readouterr()
        assert run(db_path, "remove", "a/r") == 0
        assert capsys.readouterr().out == "removed: a/r <- https://github.com/a/r\n"
        assert run(db_path, "remove", "s", "nope") == 1
        assert "'nope': no such project; nothing removed" in capsys.readouterr().err
        with GitRepoDB(db_path) as db:
            assert [n for _, n, _ in db.rows()] == ["s"]

    @pytest.mark.parametrize(
        "args",
        [
            [],
            ["--all"],
            ["--owner", "a"],
            ["a/r", "--owner", "a", "--all"],
            ["a/r", "--all"],
        ],
    )
    def test_remove_rejects_bad_arguments(self, db_path, args):
        with GitRepoDB(db_path) as db:
            db.add([("r", "https://github.com/a/r")])
        with pytest.raises(SystemExit):
            run(db_path, "remove", *args)
        with GitRepoDB(db_path) as db:
            assert len(db.rows()) == 1

    def test_remove_owner_all(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    ("r", "https://github.com/a/r"),
                    ("s", "https://github.com/A/s"),
                    ("r", "https://github.com/b/r"),
                ]
            )
        capsys.readouterr()
        assert run(db_path, "remove", "--owner", "A", "--all") == 0
        assert capsys.readouterr().out == (
            "removed: a/r <- https://github.com/a/r\n"
            "removed: A/s <- https://github.com/A/s\n"
        )
        with GitRepoDB(db_path) as db:
            assert db.rows() == [("b", "r", "https://github.com/b/r")]
        assert run(db_path, "remove", "--owner", "a", "--all") == 1
        assert "'a': no projects for this owner" in capsys.readouterr().err

    def test_clone_from_db_grouped(self, remotes, db_path, tmp_path):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
        assert run(db_path, "clone", tmp_path / "d", "-g") == 0
        assert (tmp_path / "d" / "bob" / "alpha" / "README").read_text() == "bob/alpha"
        assert (
            tmp_path / "d" / "alice" / "alpha" / "README"
        ).read_text() == "alice/alpha"

    def test_clone_from_db_flat_rejects_repeated_name(
        self, remotes, db_path, tmp_path, fake_git
    ):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
        with pytest.raises(SystemExit):
            run(db_path, "clone", tmp_path / "d")
        fake_git.assert_not_called()

    def test_clone_from_db_flat_with_owner(self, remotes, db_path, tmp_path):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
        assert run(db_path, "clone", tmp_path / "d", "--owner", "bob") == 0
        assert (tmp_path / "d" / "alpha" / "README").read_text() == "bob/alpha"

    def test_clone_from_db_reports_failures(self, remotes, db_path, tmp_path):
        with GitRepoDB(db_path) as db:
            db.add(
                [("alpha", remotes["alice", "alpha"]), ("gone", f"{HOST}x/gone.git")]
            )
        assert run(db_path, "clone", tmp_path / "d") == 1
        assert (tmp_path / "d" / "alpha" / "README").exists()

    def test_clone_from_flat_json_allows_local_urls(self, tmp_path, db_path, fake_git):
        f = write_json(tmp_path / "p.json", {"l": "/x/l.git"})
        assert run(db_path, "clone", tmp_path / "d", "--json", f) == 0
        assert cloned_targets(fake_git) == [str(tmp_path / "d" / "l")]
        assert not db_path.exists()

    def test_clone_grouped_rejects_ownerless_json(
        self, tmp_path, db_path, fake_git, capsys
    ):
        f = write_json(
            tmp_path / "p.json", {"a": "https://github.com/u/a", "l": "/x/l"}
        )
        assert run(db_path, "clone", tmp_path / "d", "-g", "--json", f) == 1
        assert "no owner in url: 'l'" in capsys.readouterr().err
        fake_git.assert_not_called()

    def test_clone_from_grouped_json(self, remotes, db_path, tmp_path):
        data = {
            "ignored": {"alpha": remotes["alice", "alpha"]},
            "keys": {"alpha": remotes["bob", "alpha"]},
        }
        f = write_json(tmp_path / "p.json", data)
        with pytest.raises(SystemExit):  # flat: both would clone to DEST/alpha
            run(db_path, "clone", tmp_path / "d", "--json", f)
        assert run(db_path, "clone", tmp_path / "d", "--json", f, "-g") == 0
        assert (
            tmp_path / "d" / "alice" / "alpha" / "README"
        ).read_text() == "alice/alpha"
        assert (tmp_path / "d" / "bob" / "alpha" / "README").read_text() == "bob/alpha"

    def test_clone_json_with_owner_is_error(self, db_path, tmp_path):
        f = write_json(tmp_path / "p.json", {})
        with pytest.raises(SystemExit):
            run(db_path, "clone", tmp_path / "d", "--json", f, "--owner", "u")

    def test_clone_repo(self, remotes, db_path, tmp_path):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
        d = tmp_path / "d"
        assert run(db_path, "clone", "-r", "beta", "-r", "bob/alpha", d) == 0
        assert sorted(p.name for p in d.iterdir()) == ["alpha", "beta"]
        assert (d / "alpha" / "README").read_text() == "bob/alpha"

    def test_clone_repo_repeated_spec(self, db_path, tmp_path, fake_git):
        with GitRepoDB(db_path) as db:
            db.add([("r", "https://github.com/a/r")])
        assert run(db_path, "clone", "-r", "r", "-r", "a/r", tmp_path / "d") == 0
        assert cloned_targets(fake_git) == [str(tmp_path / "d" / "r")]

    @pytest.mark.parametrize(
        "spec, message", [("alpha", "ambiguous"), ("nope", "no such project")]
    )
    def test_clone_repo_not_found_or_ambiguous(
        self, remotes, db_path, tmp_path, capsys, fake_git, spec, message
    ):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
        assert run(db_path, "clone", "-r", "beta", "-r", spec, tmp_path / "d") == 1
        assert message in capsys.readouterr().err
        fake_git.assert_not_called()

    @pytest.mark.parametrize(
        "args",
        [
            ["-r", "r", "--owner", "a"],
            ["-r", "r", "-t", "x"],
            ["-r", "r", "--json", "p.json"],
        ],
    )
    def test_clone_rejects_bad_selection(self, db_path, tmp_path, fake_git, args):
        with pytest.raises(SystemExit):
            run(db_path, "clone", *args, tmp_path / "d")
        fake_git.assert_not_called()

    def test_clone_topic(self, remotes, db_path, tmp_path):
        with GitRepoDB(db_path) as db:
            add_tagged(db, [(n, u, ["agent"]) for (_, n), u in remotes.items()])
        d = tmp_path / "d"
        assert run(db_path, "clone", "-t", "agent", "--owner", "alice", d) == 0
        assert sorted(p.name for p in d.iterdir()) == ["alpha", "beta"]
        assert (d / "alpha" / "README").read_text() == "alice/alpha"
        # Both alphas have the topic; flat would clone both to d/alpha.
        with pytest.raises(SystemExit):
            run(db_path, "clone", "-t", "agent", tmp_path / "e")
        assert run(db_path, "clone", "-t", "agent", "-g", tmp_path / "e") == 0
        assert (tmp_path / "e" / "bob" / "alpha" / "README").exists()

    def test_clone_topic_none_match(self, db_path, tmp_path, capsys, fake_git):
        with GitRepoDB(db_path) as db:
            db.add([("r", "https://github.com/a/r")])
        assert run(db_path, "clone", "-t", "agent", tmp_path / "d") == 1
        assert "no projects with topic 'agent'" in capsys.readouterr().err
        fake_git.assert_not_called()


class TestTopicsCommand:
    def test_counts(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            add_tagged(
                db,
                [
                    ("r", "https://github.com/a/r", ["agent", "cli"]),
                    ("s", "https://github.com/a/s", ["Agent"]),
                    ("t", "https://github.com/b/t", ["rag", "agent"]),
                ],
            )
        capsys.readouterr()
        assert run(db_path, "topics") == 0
        assert capsys.readouterr().out == "    3  agent\n    1  cli\n    1  rag\n"
        assert run(db_path, "topics", "--owner", "B") == 0
        assert capsys.readouterr().out == "    1  agent\n    1  rag\n"

    def test_none(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add([("r", "https://github.com/a/r")])
        assert run(db_path, "topics") == 1
        assert "no topics" in capsys.readouterr().err

    def test_list_topics(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add([("longname", "https://github.com/b/longname")])
            add_tagged(
                db,
                [
                    ("r", "https://github.com/a/r", ["cli", "agent"]),
                    ("s", "https://github.com/A/s", ["rag"]),
                ],
            )
        capsys.readouterr()
        assert run(db_path, "list", "--topics") == 0
        assert capsys.readouterr().out == (
            "r         agent, cli\ns         rag\nlongname\n"
        )
        assert run(db_path, "list", "--topics", "-g", "-t", "agent", "-u") == 0
        assert capsys.readouterr().out == "a\n  https://github.com/a/r  agent, cli\n"
        assert run(db_path, "list", "-g") == 0
        assert capsys.readouterr().out == "a\n  r\n  s\nb\n  longname\n"


class TestSets:
    @pytest.fixture
    def stored(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    ("gilda", "https://github.com/a/gilda"),
                    ("myra", "https://github.com/a/myra"),
                    ("r", "https://github.com/a/r"),
                    ("r", "https://github.com/b/r"),
                    ("timu", "https://gitlab.com/b/timu"),
                ]
            )
            add_tagged(db, [("myra", "https://github.com/a/myra", ["agent"])])
        capsys.readouterr()

    def out(self, capsys):
        return capsys.readouterr().out.splitlines()

    def test_add_is_idempotent_and_keeps_spelling(self, db_path, stored, capsys):
        assert run(db_path, "set", "--name", "Agents", "gilda", "a/myra") == 0
        assert self.out(capsys) == [
            "added to Agents: a/gilda",
            "added to Agents: a/myra",
        ]
        assert run(db_path, "set", "--name", "agents", "myra", "timu") == 0
        assert self.out(capsys) == ["added to agents: b/timu"]
        assert run(db_path, "set") == 0
        assert self.out(capsys) == ["    3  Agents"]
        assert run(db_path, "set", "--name", "AGENTS") == 0
        assert self.out(capsys) == ["a/gilda", "a/myra", "b/timu"]

    @pytest.mark.parametrize(
        "spec, message",
        [("r", "'r': ambiguous, use one of a/r, b/r"), ("nope", "no such project")],
    )
    def test_add_is_atomic(self, db_path, stored, capsys, spec, message):
        assert run(db_path, "set", "--name", "x", "gilda", spec) == 1
        assert f"{message}; nothing changed" in capsys.readouterr().err
        assert run(db_path, "set") == 0
        assert self.out(capsys) == []

    def test_remove_and_delete(self, db_path, stored, capsys):
        run(db_path, "set", "--name", "x", "gilda", "myra", "b/r")
        capsys.readouterr()
        assert run(db_path, "set", "--name", "x", "--remove", "myra") == 0
        assert self.out(capsys) == ["removed from x: a/myra"]
        # timu is not a member: nothing is removed, gilda stays.
        assert run(db_path, "set", "--name", "x", "--remove", "gilda", "timu") == 1
        assert "'timu': not in set 'x'; nothing changed" in capsys.readouterr().err
        assert run(db_path, "set", "--name", "x") == 0
        assert self.out(capsys) == ["a/gilda", "b/r"]
        assert run(db_path, "set", "--name", "X", "--delete") == 0
        assert self.out(capsys) == ["deleted set 'X' (2 projects)"]
        assert run(db_path, "set", "--name", "x", "--delete") == 1
        assert "'x': no such set" in capsys.readouterr().err
        assert run(db_path, "set", "--name", "x") == 1

    def test_removing_project_drops_membership(self, db_path, stored, capsys):
        run(db_path, "set", "--name", "x", "gilda", "myra")
        run(db_path, "remove", "gilda")
        run(
            db_path,
            "import",
            write_json(
                db_path.parent / "p.json", {"gilda": "https://github.com/a/gilda"}
            ),
        )
        capsys.readouterr()
        assert run(db_path, "set", "--name", "x") == 0
        assert self.out(capsys) == ["a/myra"]

    @pytest.mark.parametrize(
        "args",
        [
            ["gilda"],
            ["--delete"],
            ["--remove", "gilda"],
            ["--name", "x", "--remove"],
            ["--name", "x", "--delete", "gilda"],
            ["--name", "x", "--delete", "--remove", "gilda"],
        ],
    )
    def test_rejects_bad_arguments(self, db_path, stored, args):
        with pytest.raises(SystemExit):
            run(db_path, "set", *args)

    def test_select(self, db_path, stored, tmp_path, capsys, fake_git):
        run(db_path, "set", "--name", "agents", "gilda", "myra", "timu")
        capsys.readouterr()
        assert run(db_path, "list", "-s", "AGENTS") == 0
        assert self.out(capsys) == ["gilda", "myra", "timu"]
        assert run(db_path, "list", "-s", "agents", "--owner", "b", "--slugs") == 0
        assert self.out(capsys) == ["b/timu"]
        assert run(db_path, "list", "-s", "agents", "-t", "agent") == 0
        assert self.out(capsys) == ["myra"]
        assert run(db_path, "list", "-s", "agents", "-t", "nope") == 1
        assert (
            "no projects in set 'agents' with topic 'nope'; see 'repodb set'"
            in capsys.readouterr().err
        )
        assert run(db_path, "list", "-s", "nope") == 1
        assert "no projects in set 'nope'" in capsys.readouterr().err

        out = tmp_path / "p.json"
        assert run(db_path, "export", "-s", "agents", "-g", "-o", out) == 0
        assert list(json.loads(out.read_text())) == ["a", "b"]

        d = tmp_path / "d"
        assert run(db_path, "clone", "-s", "agents", "-j", "2", d) == 0
        assert sorted(cloned_targets(fake_git)) == [
            str(d / n) for n in ["gilda", "myra", "timu"]
        ]
        for args in (["-r", "gilda"], ["--json", out]):
            with pytest.raises(SystemExit):
                run(db_path, "clone", "-s", "agents", *args, d)

    def test_status(self, remotes, db_path, tmp_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
        run(db_path, "set", "--name", "x", "alice/alpha")
        d = tmp_path / "d"
        run(db_path, "clone", "-g", "-s", "x", d)
        capsys.readouterr()
        assert run(db_path, "status", "-g", "-s", "x", d) == 0
        assert run(db_path, "status", "-g", d) == 1
        assert self.out(capsys) == ["missing: alice/beta", "missing: bob/alpha"]


def set_origin(path, url):
    git("-C", str(path), "remote", "set-url", "origin", url)


class TestStatus:
    @pytest.fixture
    def stored(self, remotes, db_path):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
        return remotes

    def test_grouped(self, stored, db_path, tmp_path, capsys):
        d = tmp_path / "d"
        assert run(db_path, "clone", "-g", d) == 0
        capsys.readouterr()
        assert run(db_path, "status", "-g", d) == 0
        assert capsys.readouterr().out == ""
        set_origin(d / "alice" / "beta", "https://example.org/x/beta")
        shutil.rmtree(d / "bob" / "alpha")
        (d / "alice" / "extra").mkdir()
        (d / "carol").mkdir()
        (d / ".cache").mkdir()
        (d / "file").write_text("")
        assert run(db_path, "status", "-g", d) == 1
        assert capsys.readouterr().out.splitlines() == [
            (
                "differs: alice/beta (origin https://example.org/x/beta,"
                f" stored {stored['alice', 'beta']})"
            ),
            "missing: bob/alpha",
            "untracked: alice/extra",
            "untracked: carol",
        ]

    def test_flat(self, stored, db_path, tmp_path, capsys):
        d = tmp_path / "d"
        assert run(db_path, "clone", "--owner", "alice", d) == 0
        (d / "plain").mkdir()
        git("-C", str(d / "alpha"), "remote", "remove", "origin")
        capsys.readouterr()
        assert run(db_path, "status", "--owner", "alice", d) == 1
        assert capsys.readouterr().out.splitlines() == [
            "no origin: alpha",
            "untracked: plain",
        ]

    def test_filter_does_not_report_others_untracked(
        self, stored, db_path, tmp_path, capsys
    ):
        d = tmp_path / "d"
        assert run(db_path, "clone", "-g", d) == 0
        capsys.readouterr()
        # bob's clone is not in the filtered rows but is still known.
        assert run(db_path, "status", "-g", "--owner", "alice", d) == 0
        assert capsys.readouterr().out == ""

    def test_not_a_git_repo_and_bad_dest(self, stored, db_path, tmp_path, capsys):
        d = tmp_path / "d"
        (d / "bob" / "alpha").mkdir(parents=True)
        assert run(db_path, "status", "-g", "--owner", "bob", d) == 1
        assert capsys.readouterr().out == "not a git repo: bob/alpha\n"
        with pytest.raises(SystemExit):
            run(db_path, "status", tmp_path / "missing")


class TestParallelClone:
    def test_clones_and_reports(self, remotes, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="repodb")
        dest = tmp_path / "dest"
        rows = [(o, n, u) for (o, n), u in remotes.items()]
        rows.append(("x", "gone", f"{HOST}x/gone.git"))
        assert clone(rows, dest, by_owner=True, jobs=4) == ["x/gone"]
        for owner, name in remotes:
            assert (dest / owner / name / "README").read_text() == f"{owner}/{name}"
        info = [r.message for r in caplog.records if r.levelname == "INFO"]
        warn = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert sorted(info) == sorted(
            f"cloned: {o}/{n} <- {u}" for (o, n), u in remotes.items()
        )
        assert len(warn) == 1
        assert warn[0].startswith(f"clone failed: x/gone <- {HOST}x/gone.git\n")
        assert "fatal:" in warn[0]

    def test_main(self, remotes, db_path, tmp_path):
        with GitRepoDB(db_path) as db:
            db.add([(n, u) for (_, n), u in remotes.items()])
        assert run(db_path, "clone", "-g", "-j", "3", tmp_path / "d") == 0
        assert (tmp_path / "d" / "bob" / "alpha" / "README").exists()

    def test_interrupt_cancels_queued_clones(self, tmp_path):
        # Executor.map cancels queued work when its iterator raises.
        calls = []

        def interrupted(*args, **kwargs):
            calls.append(args)
            time.sleep(0.05)
            raise KeyboardInterrupt

        rows = [("o", f"r{i}", f"{HOST}o/r{i}.git") for i in range(20)]
        with (
            patch("repodb.core.subprocess.run", side_effect=interrupted),
            pytest.raises(KeyboardInterrupt),
        ):
            clone(rows, tmp_path / "d", jobs=2)
        assert len(calls) < len(rows)

    @pytest.mark.parametrize("jobs", ["0", "-1", "x"])
    def test_rejects_bad_jobs(self, db_path, tmp_path, jobs):
        with pytest.raises(SystemExit):
            run(db_path, "clone", "-j", jobs, tmp_path / "d")


GH_OUT = json.dumps(
    [
        {
            "name": "zed",
            "url": "https://github.com/u/zed",
            "sshUrl": "git@github.com:u/zed.git",
            "repositoryTopics": [{"name": "agent"}, {"name": "cli"}],
        },
        {
            "name": "abc",
            "url": "https://github.com/u/abc",
            "sshUrl": "git@github.com:u/abc.git",
            "repositoryTopics": None,
        },
    ]
)


def fake_run(stdout):
    """A subprocess.run stand-in that returns *stdout* and records its calls."""
    return Mock(
        side_effect=lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=stdout, stderr=""
        )
    )


class TestGithub:
    def test_https_sorted(self):
        fr = fake_run(GH_OUT)
        with patch("repodb.core.subprocess.run", fr):
            result = github("u", 50, ssh=False, source=True, no_archived=False)
        cmd = fr.call_args.args[0]
        assert result == [
            ("abc", "https://github.com/u/abc", ()),
            ("zed", "https://github.com/u/zed", ("agent", "cli")),
        ]
        assert cmd[:4] == ["gh", "repo", "list", "u"]
        assert "--source" in cmd and "--no-archived" not in cmd
        assert cmd[cmd.index("--json") + 1] == "name,url,repositoryTopics"

    def test_ssh(self):
        with patch("repodb.core.subprocess.run", fake_run(GH_OUT)):
            result = github("u", 50, ssh=True, source=False, no_archived=True)
        assert ("zed", "git@github.com:u/zed.git", ("agent", "cli")) in result

    @pytest.mark.parametrize("limit", ["0", "-1"])
    def test_main_rejects_bad_limit(self, db_path, limit):
        with pytest.raises(SystemExit):
            run(db_path, "github", "u", "-L", limit)

    def test_main_requires_gh(self, db_path):
        with (
            patch("repodb.cli.shutil.which", return_value=None),
            pytest.raises(SystemExit),
        ):
            run(db_path, "github", "u")

    def test_main_reports_gh_failure(self, db_path, capsys):
        err = subprocess.CalledProcessError(
            1, ["gh"], stderr="Could not resolve to a User\n"
        )
        with (
            patch("repodb.cli.shutil.which", return_value="/bin/gh"),
            patch("repodb.core.subprocess.run", side_effect=err),
        ):
            assert run(db_path, "github", "u") == 1
        assert "Could not resolve" in capsys.readouterr().err

    def test_main_stores_with_owner(self, db_path):
        with (
            patch("repodb.cli.shutil.which", return_value="/bin/gh"),
            patch("repodb.core.subprocess.run", fake_run(GH_OUT)),
        ):
            assert run(db_path, "github", "u") == 0
        with GitRepoDB(db_path) as db:
            assert db.by_owner("u") == {
                "u": {
                    "abc": "https://github.com/u/abc",
                    "zed": "https://github.com/u/zed",
                }
            }
            assert [n for _, n, _ in db.rows(topics=["agent"])] == ["zed"]

    def test_main_replaces_topics(self, db_path):
        gh_out = json.loads(GH_OUT)
        gh_out[1]["repositoryTopics"] = [{"name": "agent"}]
        gh_out[0]["repositoryTopics"] = None
        with patch("repodb.cli.shutil.which", return_value="/bin/gh"):
            with patch("repodb.core.subprocess.run", fake_run(GH_OUT)):
                assert run(db_path, "github", "u") == 0
            with patch("repodb.core.subprocess.run", fake_run(json.dumps(gh_out))):
                assert run(db_path, "github", "u") == 0
        with GitRepoDB(db_path) as db:
            assert [n for _, n, _ in db.rows(topics=["agent"])] == ["abc"]
            assert db.rows(topics=["cli"]) == []


def gh_by_owner(listings):
    """A subprocess.run stand-in for ``gh repo list OWNER``; unknown owners fail."""

    def run(cmd, **kw):
        owner = cmd[3]
        if owner not in listings:
            raise subprocess.CalledProcessError(1, cmd, stderr="Could not resolve\n")
        repos = [
            {"name": n, "url": f"https://github.com/{owner}/{n}", "repositoryTopics": t}
            for n, t in listings[owner].items()
        ]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(repos), stderr="")

    return Mock(side_effect=run)


class TestRefresh:
    def refresh(self, db_path, listings, *args):
        with (
            patch("repodb.cli.shutil.which", return_value="/bin/gh"),
            patch("repodb.core.subprocess.run", gh_by_owner(listings)) as sp,
        ):
            return run(db_path, "github", "--refresh", *args), sp

    def test_updates_stored_repos_only(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    # Scanned: directory name differs from the repo name.
                    ("mydir", "git@github.com:Alice/Real.git"),
                    ("b", "https://github.com/bob/b"),
                    ("gone", "https://github.com/bob/gone"),
                    ("g", "https://gitlab.com/alice/g"),
                ]
            )
            add_tagged(db, [("gone", "https://github.com/bob/gone", ["old"])])
        capsys.readouterr()
        listings = {
            "alice": {"real": [{"name": "agent"}], "unstored": [{"name": "agent"}]},
            "bob": {"b": None},
        }
        status, sp = self.refresh(db_path, listings)
        assert status == 0
        assert sorted(c.args[0][3] for c in sp.call_args_list) == ["alice", "bob"]
        out = capsys.readouterr()
        assert out.out == "topics: Alice/mydir <- agent\n"
        assert "not listed by gh, topics unchanged: bob/gone" in out.err
        with GitRepoDB(db_path) as db:
            assert len(db.rows()) == 4  # "unstored" was not added
            assert db.topics() == {
                ("alice", "mydir"): ["agent"],
                ("bob", "gone"): ["old"],
            }

    def test_user_filter_and_gh_failure(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    ("a", "https://github.com/alice/a"),
                    ("c", "https://github.com/carol/c"),
                ]
            )
        status, sp = self.refresh(db_path, {"alice": {"a": [{"name": "x"}]}}, "ALICE")
        assert status == 0
        assert [c.args[0][3] for c in sp.call_args_list] == ["alice"]
        status, _ = self.refresh(db_path, {"alice": {"a": [{"name": "y"}]}})
        assert status == 1
        assert "gh failed for carol: Could not resolve" in capsys.readouterr().err
        with GitRepoDB(db_path) as db:
            assert db.topics() == {("alice", "a"): ["y"]}

    @pytest.fixture
    def stored(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add(
                [
                    ("a", "https://github.com/alice/a"),
                    ("gone", "https://github.com/alice/gone"),
                    ("b", "https://github.com/bob/b"),
                    ("old", "https://github.com/bob/old"),
                    ("c", "https://github.com/carol/c"),
                ]
            )
        capsys.readouterr()

    def test_prune_lists_only(self, db_path, stored, capsys):
        listings = {"alice": {"a": None}, "bob": {"b": None}, "carol": {"c": None}}
        status, _ = self.refresh(db_path, listings, "--prune")
        assert status == 0
        out = capsys.readouterr()
        assert out.out.splitlines() == [
            "not listed by gh, would remove: alice/gone <- https://github.com/alice/gone",
            "not listed by gh, would remove: bob/old <- https://github.com/bob/old",
        ]
        assert "add --yes" in out.err
        with GitRepoDB(db_path) as db:
            assert len(db.rows()) == 5

    def test_prune_yes_skips_partial_listings(self, db_path, stored, capsys):
        # bob lists nothing (maybe no access); carol's gh call fails.
        listings = {"alice": {"a": None}, "bob": {}}
        status, _ = self.refresh(db_path, listings, "--prune", "--yes")
        assert status == 1
        out = capsys.readouterr()
        assert out.out == "removed: alice/gone <- https://github.com/alice/gone\n"
        assert "gh listed 0 repos for bob; not pruning it" in out.err
        assert "gh failed for carol" in out.err
        with GitRepoDB(db_path) as db:
            assert names(db.rows()) == ["a", "b", "old", "c"]

    def test_prune_skips_owner_at_limit(self, db_path, stored, capsys):
        listings = {"alice": {"a": None}, "bob": {"b": None}, "carol": {"c": None}}
        status, _ = self.refresh(db_path, listings, "--prune", "--yes", "-L", "1")
        assert status == 0
        err = capsys.readouterr().err
        assert "gh listed 1 repos for alice, the --limit; not pruning it" in err
        with GitRepoDB(db_path) as db:
            assert len(db.rows()) == 5

    def test_no_github_projects(self, db_path, capsys):
        with GitRepoDB(db_path) as db:
            db.add([("g", "https://gitlab.com/alice/g")])
        status, sp = self.refresh(db_path, {})
        assert status == 1
        sp.assert_not_called()
        assert "no github.com projects" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "args",
        [
            ["--refresh", "--ssh"],
            ["--refresh", "u", "--source"],
            [],
            ["u", "--prune"],
            ["--refresh", "--yes"],
        ],
    )
    def test_rejects_bad_arguments(self, db_path, args):
        with patch("repodb.core.subprocess.run") as sp, pytest.raises(SystemExit):
            run(db_path, "github", *args)
        sp.assert_not_called()


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
