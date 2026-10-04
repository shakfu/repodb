"""The library API: no printing, no argparse, results as data."""

import importlib.metadata
import json
import logging
import subprocess
from unittest.mock import Mock, patch

import pytest

import repodb
from conftest import run
from repodb import (
    Change,
    Difference,
    GitRepoDB,
    Run,
    apply_run,
    clone,
    export_projects,
    import_projects,
    publish_run,
    refresh,
    scan,
    status,
    to_json,
)
from repodb.apply import check_change


@pytest.fixture
def stored(remotes, db_path, monkeypatch):
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "t")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@t")
    with GitRepoDB(db_path) as db:
        db.add([(n, u) for (_, n), u in remotes.items()])
    return remotes


def test_public_api():
    assert all(hasattr(repodb, name) for name in repodb.__all__)
    assert repodb.main is repodb.cli.main
    assert repodb.__version__ == importlib.metadata.version("repodb")


def test_library_prints_nothing(stored, db_path, tmp_path, capsys):
    (tmp_path / "src" / "plain").mkdir(parents=True)
    with GitRepoDB(db_path) as db:
        changes = db.add([("r", "https://github.com/a/r")])
        rows = db.rows(owner="alice")
    assert changes == [Change("added", "a", "r", "https://github.com/a/r")]
    assert scan(tmp_path / "src") == []
    assert clone(rows, tmp_path / "d") == []
    assert clone(rows, tmp_path / "d") == []  # exists: skipped
    assert capsys.readouterr() == ("", "")


def test_status_returns_differences(stored, db_path, tmp_path):
    with GitRepoDB(db_path) as db:
        rows = db.rows(owner="alice")
    clone(rows[:1], tmp_path / "d")
    (tmp_path / "d" / "extra").mkdir()
    found = status(rows, tmp_path / "d")
    assert found == [Difference("missing", "beta"), Difference("untracked", "extra")]
    assert str(found[0]) == "missing: beta"


def test_export_import_roundtrip(db_path, tmp_path):
    with GitRepoDB(db_path) as db:
        db.add_projects([("r", "https://github.com/a/r", ["agent"])])
        data = export_projects(db, grouped=True)
        entry = {"url": "https://github.com/a/r", "topics": ["agent"]}
        assert data == {"a": {"r": entry}}
        with pytest.raises(ValueError, match="held by more than one owner"):
            db.add([("r", "https://github.com/b/r")])
            export_projects(db)
    f = tmp_path / "p.json"
    f.write_text(to_json(data))
    with GitRepoDB(tmp_path / "other.sqlite") as other:
        changes = import_projects(other, f)
        assert [c.action for c in changes] == ["added", "topics"]
        assert export_projects(other) == {"r": entry}


def test_refresh_reports_and_removes_nothing(db_path):
    with GitRepoDB(db_path) as db:
        db.add(
            [
                ("a", "https://github.com/alice/a"),
                ("gone", "https://github.com/alice/gone"),
                ("b", "https://github.com/bob/b"),
                ("g", "https://gitlab.com/carol/g"),
            ]
        )

        def gh(cmd, **kw):
            listing = {
                "alice": [
                    {"name": "a", "url": "u", "repositoryTopics": [{"name": "x"}]}
                ],
                "bob": [],
            }[cmd[3]]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(listing))

        with patch("repodb.core.subprocess.run", Mock(side_effect=gh)):
            r = refresh(db)
        assert r.checked == 3
        assert [str(c) for c in r.changes] == ["topics: alice/a <- x"]
        assert r.unlisted == [
            ("alice", "gone", "https://github.com/alice/gone"),
            ("bob", "b", "https://github.com/bob/b"),
        ]
        assert r.prunable == [("alice", "gone", "https://github.com/alice/gone")]
        assert r.partial == {"bob": 0}
        assert len(db.rows()) == 4


def test_run_lifecycle(stored, db_path, tmp_path):
    run_ = Run.create("shout", "Shout", replace=["alice", "ALICE"], glob=["README"])
    assert run_.branch == "repodb/shout"
    with GitRepoDB(db_path) as db:
        assert run_.add(db.rows(owner="alice")) == ["alice/alpha", "alice/beta"]
    root = tmp_path / "runs" / "shout"
    seen = []
    assert apply_run(run_, root, jobs=2, on_done=lambda s, r: seen.append((s, r.state)))
    assert sorted(seen) == [("alice/alpha", "committed"), ("alice/beta", "committed")]
    assert Run.load(root).counts() == {"committed": 2}
    assert publish_run(run_, root, ["alice/beta"], push_default=True) == ["alice/beta"]
    assert run_.counts() == {"committed": 1, "published": 1}
    with pytest.raises(ValueError, match="already published: alice/beta"):
        run_.redo()
    assert run_.resolve(["alpha"]) == ["alice/alpha"]


@pytest.mark.parametrize(
    "exec_, replace, glob, message",
    [
        ("true", ["a", "b"], ["x"], "one of exec, replace or script"),
        (None, ["a", "b"], [], "go together"),
        (None, None, ["x"], "go together"),
        (None, ["(", "b"], ["x"], "replace pattern"),
        (None, ["a", "b"], ["../x"], "inside the repo"),
    ],
)
def test_check_change(exec_, replace, glob, message):
    with pytest.raises(ValueError, match=message):
        check_change(exec_, replace, glob)


def test_run_create_validates():
    with pytest.raises(ValueError, match="invalid run name"):
        Run.create("a/b", "m", exec="true")
    with pytest.raises(ValueError, match="needs exec, replace or script"):
        Run.create("a", "m")
    with pytest.raises(ValueError, match="commit message"):
        Run.create("a", "", exec="true")
    for branch in ["a..b", "-x", "x.lock", "@{-1}", "a b"]:
        with pytest.raises(ValueError, match="invalid branch name"):
            Run.create("a", "m", exec="true", branch=branch)
    with pytest.raises(ValueError, match="invalid branch name 'repodb/a..b'"):
        Run.create("a..b", "m", exec="true")
    assert Run.create("a", "m", exec="true", branch="x/y").branch == "x/y"


def test_cli_logging_is_scoped(db_path, capsys):
    GitRepoDB(db_path).close()
    log = logging.getLogger("repodb")
    before = (list(log.handlers), log.level, log.propagate)
    run(db_path, "list")
    assert (list(log.handlers), log.level, log.propagate) == before
