"""Fixtures shared by the test modules."""

import subprocess

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
