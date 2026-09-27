"""Tests for repodb.apply: apply, review, publish, runs."""

import json
import re
import subprocess
from unittest.mock import patch

import pytest

from conftest import git, run
from repodb.apply import Run, prepare, replace_in
from repodb.core import GitRepoDB


@pytest.fixture
def ident(monkeypatch):
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "t")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@t")


@pytest.fixture
def stored(remotes, db_path, ident):
    with GitRepoDB(db_path) as db:
        db.add([(n, u) for (_, n), u in remotes.items()])
    return remotes


def runs(tmp_path):
    return tmp_path / "state" / "runs"


def load(tmp_path, name):
    return Run.load(runs(tmp_path) / name)


def bare(tmp_path, owner, name):
    return tmp_path / "remotes" / owner / f"{name}.git"


def head(repo, ref="HEAD"):
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else None


SHOUT = ["--replace", "alice", "ALICE", "--glob", "README", "-m", "Shout\n\nBody."]


class TestApply:
    def test_replace_commits_and_reviews(self, stored, db_path, tmp_path, capsys):
        assert run(db_path, "apply", "shout", *SHOUT, "--all", "-j", "3") == 0
        out = capsys.readouterr().out
        assert "committed: alice/alpha (1 file changed" in out
        assert "unchanged: bob/alpha" in out
        assert "run shout: committed 2, unchanged 1" in out
        r = load(tmp_path, "shout")
        assert r.branch == "repodb/shout"
        alpha = r.repos["alice/alpha"]
        assert (alpha.files, alpha.matches) == (["README"], {"README": 1})
        clone = runs(tmp_path) / "shout" / "repos" / "alice" / "alpha"
        assert (clone / "README").read_text() == "ALICE/alpha"
        assert head(clone, "repodb/shout") == alpha.commit
        assert run(db_path, "review", "shout") == 0
        stat = "1 file changed, 1 insertion(+), 1 deletion(-); 1 matches"
        assert capsys.readouterr().out.splitlines() == [
            f"committed  alice/alpha  {stat}",
            f"committed  alice/beta  {stat}",
            "unchanged  bob/alpha",
        ]

    def test_review_diff(self, stored, db_path, tmp_path, capfd):
        run(db_path, "apply", "shout", *SHOUT, "-r", "alice/alpha")
        capfd.readouterr()
        assert run(db_path, "review", "shout", "alpha", "--diff") == 0
        out = capfd.readouterr().out
        assert "== alice/alpha" in out
        assert "+ALICE/alpha" in out

    def test_exec_env_and_new_files(self, stored, db_path, tmp_path):
        cmd = 'echo "$REPODB_RUN $REPODB_OWNER/$REPODB_NAME" > NEW.txt'
        assert run(db_path, "apply", "env", "--exec", cmd, "-m", "m", "-s", "x") == 1
        run(db_path, "set", "--name", "x", "bob/alpha")
        assert run(db_path, "apply", "env", "--exec", cmd, "-m", "m", "-s", "x") == 0
        r = load(tmp_path, "env")
        assert list(r.repos) == ["bob/alpha"]
        assert r.repos["bob/alpha"].files == ["NEW.txt"]
        clone = runs(tmp_path) / "env" / "repos" / "bob" / "alpha"
        assert (clone / "NEW.txt").read_text() == "env bob/alpha\n"

    def test_failure_then_resume(self, stored, db_path, tmp_path, capsys):
        marker = tmp_path / "ok"
        cmd = f"test -f {marker} && echo x >> README"
        args = ["apply", "r", "--exec", cmd, "-m", "m", "--owner", "alice"]
        assert run(db_path, *args) == 1
        err = capsys.readouterr().err
        assert "failed: alice/alpha (exec exited 1; see " in err
        assert load(tmp_path, "r").repos["alice/beta"].state == "failed"
        marker.touch()
        assert run(db_path, "apply", "r") == 0  # resumes with the stored change
        assert {s.state for s in load(tmp_path, "r").repos.values()} == {"committed"}

    def test_timeout(self, stored, db_path, tmp_path, capsys):
        args = ["apply", "t", "--exec", "sleep 30", "-m", "m", "-r", "beta"]
        assert run(db_path, *args, "--timeout", "1") == 1
        assert "exec timed out after 1s" in capsys.readouterr().err

    def test_changed_change_needs_redo(self, stored, db_path, tmp_path, capsys):
        run(db_path, "apply", "s", *SHOUT, "-r", "beta")
        other = ["--replace", "beta", "BETA", "--glob", "README", "-m", "m2"]
        with pytest.raises(SystemExit):
            run(db_path, "apply", "s", *other)
        assert "add --redo" in capsys.readouterr().err
        assert run(db_path, "apply", "s", *other, "--redo") == 0
        r = load(tmp_path, "s")
        assert (r.replace, r.message) == (["beta", "BETA"], "m2")
        assert r.repos["alice/beta"].matches == {"README": 1}
        with pytest.raises(SystemExit):
            run(db_path, "apply", "s", "--branch", "other")

    def test_redo_refuses_published(self, stored, db_path, tmp_path, capsys):
        run(db_path, "apply", "s", *SHOUT, "-r", "beta")
        run(db_path, "publish", "s", "--push-default")
        capsys.readouterr()
        assert run(db_path, "apply", "s", "--redo", "beta") == 1
        assert "already published" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "args",
        [
            ["bad/name", *SHOUT, "--all"],
            ["r", *SHOUT],  # no selection
            ["r", "-m", "m", "--all"],  # no change
            ["r", "--exec", "true", "--all"],  # no message
            ["r", "--replace", "a", "b", "-m", "m", "--all"],  # no --glob
            ["r", "--exec", "true", "--glob", "x", "-m", "m", "--all"],
            ["r", "--replace", "(", "b", "--glob", "x", "-m", "m", "--all"],
            ["r", "--replace", "a", "b", "--glob", "../x", "-m", "m", "--all"],
            ["r", "--replace", "a", "b", "--glob", "/etc/*", "-m", "m", "--all"],
            ["r", "--exec", "true", "--replace", "a", "b", "-m", "m", "--all"],
        ],
    )
    def test_rejects_bad_arguments(self, stored, db_path, tmp_path, args):
        with pytest.raises(SystemExit):
            run(db_path, "apply", *args)
        assert not (runs(tmp_path) / "r").exists()

    def test_unknown_selection(self, stored, db_path, tmp_path, capsys):
        assert run(db_path, "apply", "r", *SHOUT, "-r", "nope") == 1
        assert run(db_path, "apply", "r", *SHOUT, "-s", "nope") == 1
        assert "no projects in set 'nope'" in capsys.readouterr().err


class TestRunAdd:
    def new(self):
        return Run.create("r", "m", exec="true")

    @pytest.mark.parametrize(
        "owner, name", [("..", "r"), ("a", ".."), ("a", "../../victim"), ("a", "b/c")]
    )
    def test_rejects_unsafe_owner_or_name(self, owner, name):
        r = self.new()
        with pytest.raises(ValueError, match="invalid owner or name"):
            r.add([("a", "ok", "https://github.com/a/ok"), (owner, name, "u")])
        assert r.repos == {}

    def test_skips_same_repo_under_two_names(self, caplog):
        r = self.new()
        rows = [
            ("a", "mydir", "git@github.com:a/real.git"),
            ("a", "real", "https://github.com/a/real"),
        ]
        assert r.add(rows) == ["a/mydir"]
        assert "same repo as a/mydir, skipping: a/real" in caplog.text
        # A later add is checked against repos already in the run.
        assert r.add(rows[1:]) == []
        assert list(r.repos) == ["a/mydir"]

    def test_prepare_rejects_edited_slug(self, tmp_path):
        victim = tmp_path / "victim"
        victim.mkdir()
        root = tmp_path / "runs" / "r"
        repo = prepare(self.new(), "../../victim", "u", root, None)
        assert (repo.state, repo.error) == (
            "failed",
            "invalid owner or name: '../../victim'",
        )
        assert victim.exists()


class TestReplaceIn:
    @pytest.fixture
    def repo(self, tmp_path):
        r = tmp_path / "r"
        (r / "sub").mkdir(parents=True)
        (r / "a.txt").write_bytes(b"x1\r\nx2\r\n")
        (r / "sub" / "b.txt").write_text("x")
        (r / "bin.txt").write_bytes(b"x\xff")
        (r / "link.txt").symlink_to(tmp_path / "outside.txt")
        (tmp_path / "outside.txt").write_text("x")
        git("init", "-q", str(r))
        git("-C", str(r), "add", ".")
        (r / "untracked.txt").write_text("x")
        return r

    def test_counts_and_skips(self, repo, tmp_path):
        counts = replace_in(repo, re.compile(r"x(\d)?"), r"y\1", ["*.txt", "**/*.txt"])
        assert counts == {"a.txt": 2, "sub/b.txt": 1}
        assert (repo / "a.txt").read_bytes() == b"y1\r\ny2\r\n"  # CRLF kept
        assert (repo / "untracked.txt").read_text() == "x"
        assert (tmp_path / "outside.txt").read_text() == "x"
        assert (repo / "bin.txt").read_bytes() == b"x\xff"


class TestPublish:
    def test_push_default(self, stored, db_path, tmp_path, capsys):
        run(db_path, "apply", "s", *SHOUT, "--owner", "alice")
        capsys.readouterr()
        assert run(db_path, "publish", "s", "--push-default", "alpha") == 0
        r = load(tmp_path, "s")
        alpha, beta = r.repos["alice/alpha"], r.repos["alice/beta"]
        assert (alpha.state, beta.state) == ("published", "committed")
        remote = bare(tmp_path, "alice", "alpha")
        assert head(remote, alpha.default_branch) == alpha.commit
        assert f"published: alice/alpha (pushed to {alpha.default_branch})" in (
            capsys.readouterr().out
        )
        assert run(db_path, "publish", "s", "--push-default") == 0
        assert load(tmp_path, "s").repos["alice/beta"].state == "published"
        assert run(db_path, "publish", "s", "--push-default") == 0
        assert "nothing to publish" in capsys.readouterr().err

    def test_origin_moved(self, stored, db_path, tmp_path, capsys):
        run(db_path, "apply", "s", *SHOUT, "-r", "alice/alpha")
        # Someone else pushes to the default branch after apply.
        other = tmp_path / "other"
        git("clone", "-q", stored["alice", "alpha"], str(other))
        (other / "more").write_text("x")
        git("-C", str(other), "add", ".")
        git("-C", str(other), "commit", "-qm", "more")
        git("-C", str(other), "push", "-q")
        capsys.readouterr()
        assert run(db_path, "publish", "s", "--push-default") == 1
        assert "moved since apply; redo alice/alpha" in (capsys.readouterr().err)
        a = load(tmp_path, "s").repos["alice/alpha"]
        assert a.state == "committed" and "moved" in a.error
        assert run(db_path, "apply", "s", "--redo", "alpha") == 0
        assert run(db_path, "publish", "s", "--push-default") == 0

    def test_pr_needs_github(self, stored, db_path, tmp_path, capsys):
        run(db_path, "apply", "s", *SHOUT, "-r", "beta")
        with patch("repodb.cli.shutil.which", return_value="/bin/gh"):
            assert run(db_path, "publish", "s") == 1
        assert "PR mode needs a github.com url" in capsys.readouterr().err

    @pytest.fixture
    def github(self, remotes, db_path, ident, tmp_path, monkeypatch, capsys):
        """alice/alpha stored as https://github.com/..., mapped to its bare repo."""
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        monkeypatch.setenv("GIT_CONFIG_KEY_1", f"url.{tmp_path / 'remotes'}/.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_1", "https://github.com/")
        with GitRepoDB(db_path) as db:
            db.add([("alpha", "https://github.com/alice/alpha.git")])
        capsys.readouterr()
        calls = []
        real = subprocess.run

        def fake(cmd, *a, **kw):
            if cmd[0] != "gh":
                return real(cmd, *a, **kw)
            calls.append(cmd)
            out = (
                "Token scopes: 'repo'"
                if cmd[1] == "auth"
                else "https://github.com/alice/alpha/pull/7\n"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        with (
            patch("repodb.cli.shutil.which", return_value="/bin/gh"),
            patch("repodb.apply.subprocess.run", fake),
        ):
            yield calls

    def test_pr(self, github, db_path, tmp_path, capsys):
        run(db_path, "apply", "s", *SHOUT, "-r", "alpha")
        assert run(db_path, "publish", "s", "--draft") == 0
        a = load(tmp_path, "s").repos["alice/alpha"]
        assert (a.state, a.pr) == ("published", "https://github.com/alice/alpha/pull/7")
        assert head(bare(tmp_path, "alice", "alpha"), "repodb/s") == a.commit
        assert github == [
            [
                "gh",
                "pr",
                "create",
                "--repo",
                "alice/alpha",
                "--base",
                a.default_branch,
                "--head",
                "repodb/s",
                "--title",
                "Shout",
                "--body",
                "Body.",
                "--draft",
            ]
        ]

    def test_workflow_scope_checked(self, github, db_path, tmp_path, capsys):
        cmd = "mkdir -p .github/workflows && echo x > .github/workflows/ci.yml"
        run(db_path, "apply", "w", "--exec", cmd, "-m", "m", "-r", "alpha")
        capsys.readouterr()
        assert run(db_path, "publish", "w") == 1
        assert "gh auth refresh -s workflow" in capsys.readouterr().err
        assert [c[1] for c in github] == ["auth"]
        assert head(bare(tmp_path, "alice", "alpha"), "repodb/w") is None


class TestRuns:
    def test_list_and_discard(self, stored, db_path, tmp_path, capsys):
        run(db_path, "apply", "one", *SHOUT, "--all")
        run(db_path, "apply", "two", "--exec", "exit 1", "-m", "m", "-r", "beta")
        (runs(tmp_path) / "junk").mkdir()
        capsys.readouterr()
        assert run(db_path, "runs") == 0
        assert capsys.readouterr().out == (
            "one  committed 2, unchanged 1\ntwo  failed 1\n"
        )
        assert run(db_path, "runs", "one", "--discard") == 0
        assert not (runs(tmp_path) / "one").exists()
        with pytest.raises(SystemExit):
            run(db_path, "review", "one")
        with pytest.raises(SystemExit):
            run(db_path, "runs", "--discard")

    def test_unreadable_manifest(self, stored, db_path, tmp_path):
        run(db_path, "apply", "one", *SHOUT, "-r", "beta")
        manifest = runs(tmp_path) / "one" / "run.json"
        manifest.write_text(json.dumps({"name": "one"}))
        with pytest.raises(SystemExit) as e:
            run(db_path, "review", "one")
        assert "unreadable" in str(e.value.code)

    def test_workdir(self, stored, db_path, tmp_path):
        w = tmp_path / "w"
        assert run(db_path, "apply", "one", *SHOUT, "-r", "beta", "--workdir", w) == 0
        assert Run.load(w / "one").repos["alice/beta"].state == "committed"
        assert not runs(tmp_path).exists()
