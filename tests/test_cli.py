"""Tests for the commands, mostly through repodb.cli: clone, status, sets, topics, github."""

import json
import logging
import re
import shutil
import subprocess
import time
from unittest.mock import Mock, patch

import pytest

from conftest import HOST, add_tagged, cloned_targets, git, names, run, write_json
from repodb.core import (
    GitRepoDB,
    clone,
    github,
)


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

    def test_same_repo_other_url_form(self, stored, db_path, tmp_path, capsys):
        """An origin naming the stored repo in another form is not a difference."""
        d = tmp_path / "d"
        assert run(db_path, "clone", "-r", "alice/beta", d) == 0
        set_origin(d / "beta", "git@git.example.com:Alice/beta")
        capsys.readouterr()
        assert run(db_path, "status", "--owner", "alice", d) == 1
        assert capsys.readouterr().out.splitlines() == ["missing: alpha"]

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


def gh_by_owner(listings, api=None, login="alice"):
    """A subprocess.run stand-in for ``gh repo list OWNER``; unknown owners fail.

    ``gh api repos/SLUG`` gives *api*[SLUG]: a full name, or an HTTP status
    (default 404). ``gh api user`` gives *login*.
    """

    def run(cmd, **kw):
        if cmd[:2] == ["gh", "api"]:
            if cmd[2] == "user":
                return subprocess.CompletedProcess(cmd, 0, f"{login}\n", "")
            got = (api or {}).get(cmd[2].removeprefix("repos/"), 404)
            if isinstance(got, str):
                return subprocess.CompletedProcess(cmd, 0, f"{got}\n", "")
            return subprocess.CompletedProcess(cmd, 1, "", f"gh: Error (HTTP {got})\n")
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
    def refresh(self, db_path, listings, *args, api=None):
        with (
            patch("repodb.cli.shutil.which", return_value="/bin/gh"),
            patch("repodb.core.subprocess.run", gh_by_owner(listings, api)) as sp,
        ):
            return run(db_path, "github", "--refresh", *args), sp

    @staticmethod
    def listed(sp):
        """The owners `gh repo list` was called for."""
        return [c.args[0][3] for c in sp.call_args_list if c.args[0][1] == "repo"]

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
        assert sorted(self.listed(sp)) == ["alice", "bob"]
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
        assert self.listed(sp) == ["alice"]
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
        status, sp = self.refresh(db_path, listings, "--prune")
        assert status == 0
        out = capsys.readouterr()
        # alice is the gh login, so a 404 for her repo means it is gone. bob's
        # may be private and invisible to this token.
        assert out.out.splitlines() == [
            "not listed by gh, would remove: alice/gone <- https://github.com/alice/gone",
        ]
        assert (
            "not listed by gh, topics unchanged: bob/old (not found; a private repo"
            " this token cannot see is not found either; remove it with"
            " 'repodb remove' if it is gone)"
        ) in out.err
        assert "add --yes" in out.err
        apis = [c.args[0][2] for c in sp.call_args_list if c.args[0][1] == "api"]
        assert sorted(apis) == ["repos/alice/gone", "repos/bob/old", "user"]
        with GitRepoDB(db_path) as db:
            assert len(db.rows()) == 5

    @pytest.mark.parametrize(
        "api, reason",
        [
            ("alice/renamed", "renamed to alice/renamed"),
            ("Alice/Gone", "exists, but gh did not list it"),
            (403, "gh api failed (HTTP 403)"),
        ],
    )
    def test_prune_keeps_unconfirmed(self, db_path, stored, capsys, api, reason):
        listings = {"alice": {"a": None}, "bob": {"b": None, "old": None}}
        status, _ = self.refresh(
            db_path, listings, "ALICE", "--prune", "--yes", api={"alice/gone": api}
        )
        assert status == 0
        out = capsys.readouterr()
        assert "removed" not in out.out
        assert f"not listed by gh, topics unchanged: alice/gone ({reason})" in out.err
        with GitRepoDB(db_path) as db:
            assert len(db.rows()) == 5

    def test_unlisted_without_prune_is_explained(self, db_path, stored, capsys):
        listings = {"alice": {"a": None}, "bob": {"b": None, "old": None}}
        api = {"alice/gone": "alice/new"}
        self.refresh(db_path, listings, "alice", api=api)
        err = capsys.readouterr().err
        assert "topics unchanged: alice/gone (renamed to alice/new)" in err

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
