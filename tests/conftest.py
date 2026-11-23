"""Fixtures shared by the test modules."""

import json
import subprocess
from unittest.mock import patch

import pytest

from repodb.cli import main

HOST = "https://git.example.com/"


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


@pytest.fixture(autouse=True)
def no_user_git_config(monkeypatch):
    """Ignore the user's git config, e.g. ``commit.gpgsign`` or ``url.insteadOf``."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


@pytest.fixture
def remotes(tmp_path, monkeypatch):
    """Bare repos alice/alpha, alice/beta, bob/alpha, reachable at HOST/<owner>/<name>.git.

    git's url.<base>.insteadOf, set through GIT_CONFIG_* env vars, rewrites
    HOST to the local directory, so clones are real but need no network.
    """
    root = tmp_path / "remotes"
    urls = {}
    for owner, name in [("alice", "alpha"), ("alice", "beta"), ("bob", "alpha")]:
        work = tmp_path / "work" / owner / name
        work.mkdir(parents=True)
        git("-C", str(work), "init", "-q")
        (work / "README").write_text(f"{owner}/{name}")
        git("-C", str(work), "add", ".")
        git(
            "-C",
            str(work),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "init",
        )
        git("clone", "-q", "--bare", str(work), str(root / owner / f"{name}.git"))
        urls[owner, name] = f"{HOST}{owner}/{name}.git"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{root}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", HOST)
    return urls


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state" / "repos.sqlite"


def run(db_path, *args):
    return main(["--db", str(db_path), *map(str, args)])


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
