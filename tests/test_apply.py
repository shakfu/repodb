"""Tests for repodb.apply: apply, review, publish, runs."""

import importlib.util
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import git, run
from repodb import apply
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

    def test_unexpected_error_keeps_other_results(self, stored, db_path, tmp_path):
        real = apply.replace_in

        def flaky(root, *a):
            if root.name == "alpha":
                raise OSError("disk full")
            return real(root, *a)

        with patch("repodb.apply.replace_in", flaky):
            assert (
                run(db_path, "apply", "s", *SHOUT, "--owner", "alice", "-j", "2") == 1
            )
        r = load(tmp_path, "s")
        alpha, beta = r.repos["alice/alpha"], r.repos["alice/beta"]
        assert (alpha.state, alpha.error) == ("failed", "OSError: disk full")
        assert beta.state == "committed"

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
            ["r", *SHOUT, "--all", "--branch", "a..b"],
            ["r", *SHOUT, "--all", "--branch", "-x"],
            ["a..b", *SHOUT, "--all"],  # invalid default branch repodb/a..b
            ["r", *SHOUT, "-r", "beta", "-s", "x"],  # -r goes alone
            ["r", *SHOUT, "-r", "beta", "--all"],
        ],
    )
    def test_rejects_bad_arguments(self, stored, db_path, tmp_path, args):
        with pytest.raises(SystemExit):
            run(db_path, "apply", *args)
        assert not (runs(tmp_path) / "r").exists()
        assert not (runs(tmp_path) / "a..b").exists()

    def test_unknown_selection(self, stored, db_path, tmp_path, capsys):
        assert run(db_path, "apply", "r", *SHOUT, "-r", "nope") == 1
        assert run(db_path, "apply", "r", *SHOUT, "-s", "nope") == 1
        assert "no projects in set 'nope'" in capsys.readouterr().err
        assert run(db_path, "apply", "r", *SHOUT, "--owner", "nobody") == 1
        assert "no projects owned by 'nobody'" in capsys.readouterr().err
        assert not (runs(tmp_path) / "r").exists()

    def test_empty_database(self, db_path, tmp_path, capsys):
        GitRepoDB(db_path).close()
        assert run(db_path, "apply", "r", *SHOUT, "--all") == 1
        assert "no projects; nothing to apply" in capsys.readouterr().err
        assert not (runs(tmp_path) / "r").exists()


class TestScript:
    def write(self, path, body):
        path.write_text(f"#!/bin/sh\n{body}\n")
        return path

    @pytest.mark.parametrize("kind, shebang", [("sh", "#!/bin/sh"), ("py", "#!/usr/")])
    def test_template_command(self, db_path, capsys, kind, shebang):
        assert run(db_path, "template", kind) == 0
        assert capsys.readouterr().out == apply.template(kind)
        assert apply.template(kind).startswith(shebang)

    @pytest.mark.parametrize("kind", ["sh", "py"])
    def test_unedited_template_fails(self, stored, db_path, tmp_path, kind):
        script = tmp_path / f"change.{kind}"
        script.write_text(apply.template(kind))
        assert (
            run(db_path, "apply", "t", "--script", script, "-m", "m", "-r", "beta") == 1
        )
        beta = load(tmp_path, "t").repos["alice/beta"]
        assert beta.state == "failed"
        log = runs(tmp_path) / "t" / "logs" / "alice__beta.log"
        assert "replace this line with the change" in log.read_text()

    def test_relative_paths_env_and_edits(
        self, stored, db_path, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        self.write(tmp_path / "s.sh", 'echo "$REPODB_OWNER/$REPODB_NAME v1" > who')
        args = ["apply", "s", "--workdir", "w", "-m", "m", "-r", "beta"]
        assert run(db_path, *args, "--script", "s.sh") == 0
        root = tmp_path / "w" / "s"
        beta = Run.load(root).repos["alice/beta"]
        assert (beta.state, beta.files) == ("committed", ["who"])
        assert (root / "repos/alice/beta/who").read_text() == "alice/beta v1\n"
        assert (root / "script").read_bytes() == (tmp_path / "s.sh").read_bytes()
        assert Run.load(root).script == apply.check_script(
            (root / "script").read_bytes()
        )

        # An edited script is a changed change; the stored copy is unchanged.
        self.write(tmp_path / "s.sh", 'echo "$REPODB_NAME v2" > who')
        with pytest.raises(SystemExit):
            run(db_path, *args, "--script", "s.sh")
        assert "add --redo" in capsys.readouterr().err
        assert b"v1" in (root / "script").read_bytes()
        assert run(db_path, "apply", "s", "--workdir", "w") == 0  # resumes with v1
        assert run(db_path, *args, "--script", "s.sh", "--redo") == 0
        assert (root / "repos/alice/beta/who").read_text() == "beta v2\n"

    def test_rejects_bad_script(self, stored, db_path, tmp_path, capsys):
        bare_script = tmp_path / "s.sh"
        bare_script.write_text("echo hi\n")
        for extra in [[bare_script], [tmp_path / "missing"]]:
            with pytest.raises(SystemExit):
                run(db_path, "apply", "s", "-m", "m", "--all", "--script", *extra)
        with pytest.raises(SystemExit):
            script = self.write(tmp_path / "ok.sh", "true")
            run(
                db_path,
                "apply",
                "s",
                "-m",
                "m",
                "--all",
                "--script",
                script,
                "--exec",
                "true",
            )
        assert "#! line" in capsys.readouterr().err
        assert not (runs(tmp_path) / "s").exists()

    def test_check_change_one_kind(self):
        with pytest.raises(ValueError, match="one of exec, replace or script"):
            apply.check_change("true", None, [], "abc")


def test_python_template_substitute_keeps_line_endings(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "change", Path(apply.__file__).parent / "templates" / "change.py"
    )
    assert spec is not None and spec.loader is not None
    change = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(change)
    f = tmp_path / "a.txt"
    f.write_bytes(b"old\r\nold\r\n")
    assert change.substitute(f, "old", "new") == 2
    assert f.read_bytes() == b"new\r\nnew\r\n"
    assert change.substitute(tmp_path / "missing", "old", "new") == 0


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

    def test_unexpected_error_keeps_other_results(self, stored, db_path, tmp_path):
        run(db_path, "apply", "s", *SHOUT, "--owner", "alice")
        real = apply.git

        def flaky(repo, *args):
            if args[0] == "push" and repo.name == "alpha":
                raise OSError("disk full")
            return real(repo, *args)

        with patch("repodb.apply.git", flaky):
            assert run(db_path, "publish", "s", "--push-default", "-j", "2") == 1
        r = load(tmp_path, "s")
        alpha, beta = r.repos["alice/alpha"], r.repos["alice/beta"]
        assert (alpha.state, alpha.error) == ("committed", "OSError: disk full")
        assert beta.state == "published"

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
                json.dumps({"hosts": {"github.com": [{"scopes": "repo"}]}})
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

    def test_pr_already_open(self, github, db_path, tmp_path):
        """A PR opened by an earlier publish whose result was not saved."""
        run(db_path, "apply", "s", *SHOUT, "-r", "alpha")
        fake = subprocess.run  # the github fixture's fake

        def exists(cmd, *a, **kw):
            if cmd[:3] == ["gh", "pr", "create"]:
                github.append(cmd)
                return subprocess.CompletedProcess(cmd, 1, "", "already exists")
            return fake(cmd, *a, **kw)

        with patch("repodb.apply.subprocess.run", exists):
            assert run(db_path, "publish", "s") == 0
        a = load(tmp_path, "s").repos["alice/alpha"]
        assert (a.state, a.pr) == ("published", "https://github.com/alice/alpha/pull/7")
        assert [c[2] for c in github] == ["create", "list"]
        assert github[1][3:] == [
            "--repo",
            "alice/alpha",
            "--base",
            a.default_branch,
            "--head",
            "repodb/s",
            "--json",
            "url",
            "-q",
            ".[0].url",
        ]

    def test_pr_create_fails(self, github, db_path, tmp_path, capsys):
        run(db_path, "apply", "s", *SHOUT, "-r", "alpha")
        fake = subprocess.run  # the github fixture's fake

        def fails(cmd, *a, **kw):
            if cmd[:2] == ["gh", "pr"]:
                out = "" if cmd[2] == "list" else "x"
                return subprocess.CompletedProcess(cmd, cmd[2] != "list", out, "denied")
            return fake(cmd, *a, **kw)

        capsys.readouterr()
        with patch("repodb.apply.subprocess.run", fails):
            assert run(db_path, "publish", "s") == 1
        assert "gh pr create: denied" in capsys.readouterr().err
        a = load(tmp_path, "s").repos["alice/alpha"]
        assert (a.state, a.pr) == ("committed", None)

    def test_workflow_scope_checked(self, github, db_path, tmp_path, capsys):
        cmd = "mkdir -p .github/workflows && echo x > .github/workflows/ci.yml"
        run(db_path, "apply", "w", "--exec", cmd, "-m", "m", "-r", "alpha")
        capsys.readouterr()
        assert run(db_path, "publish", "w") == 1
        assert "gh auth refresh -s workflow" in capsys.readouterr().err
        assert [c[1] for c in github] == ["auth"]
        assert head(bare(tmp_path, "alice", "alpha"), "repodb/w") is None


def gh_auth(json_out, text_out=""):
    """Fake subprocess.run for `gh auth status`; *json_out* "" means no --json."""

    def fake(cmd, *a, **kw):
        if "--json" in cmd:
            if not json_out:
                return subprocess.CompletedProcess(cmd, 1, "", "unknown flag: --json")
            return subprocess.CompletedProcess(cmd, 0, json_out, "")
        return subprocess.CompletedProcess(cmd, 0, text_out, "")

    return patch("repodb.apply.subprocess.run", fake)


def accounts(*scopes):
    hosts = [{"login": "workflow-bot", "scopes": s} for s in scopes]
    return json.dumps({"hosts": {"github.com": hosts}})


@pytest.mark.parametrize(
    "json_out, text_out, expected",
    [
        (accounts("repo, workflow"), "", True),
        (accounts("repo, read:org"), "", False),  # "workflow" only in the login
        (accounts(""), "", False),
        ("", "Token scopes: 'repo', 'workflow'", True),
        ("", "Token scopes: 'repo'", False),
    ],
)
def test_has_workflow_scope(json_out, text_out, expected):
    with gh_auth(json_out, text_out):
        assert apply.has_workflow_scope() is expected


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

    def test_format_version(self, tmp_path):
        Run.create("r", "m", exec="true").save(tmp_path)
        manifest = tmp_path / "run.json"
        data = json.loads(manifest.read_text())
        assert data["version"] == apply.RUN_VERSION
        data["later"] = 1
        data["repos"] = {"a/b": {"url": "u", "later": 1}}
        manifest.write_text(json.dumps(data))
        assert Run.load(tmp_path).repos["a/b"].url == "u"
        data["version"] = apply.RUN_VERSION + 1
        manifest.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="newer"):
            Run.load(tmp_path)

    def test_lock(self, stored, db_path, tmp_path):
        run(db_path, "apply", "one", *SHOUT, "-r", "beta")
        root = runs(tmp_path) / "one"
        with apply.locked(root):
            for args in [
                ["apply", "one", "--redo"],
                ["publish", "one", "--push-default"],
                ["runs", "one", "--discard"],
            ]:
                with pytest.raises(SystemExit) as e:
                    run(db_path, *args)
                assert "in use by another process" in str(e.value.code)
        assert run(db_path, "runs", "one", "--discard") == 0
        assert not root.exists()
        with pytest.raises(SystemExit):
            run(db_path, "publish", "nope")
        assert not (runs(tmp_path) / "nope").exists()
