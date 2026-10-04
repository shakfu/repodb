"""repodb - keep a SQLite database of git project clone URLs; clone, compare and
change the projects in bulk.

Library use::

    from repodb import GitRepoDB, clone

    with GitRepoDB() as db:
        failed = clone(db.rows(set_name="agents"), Path("~/src").expanduser(), jobs=4)

`repodb.core` holds the database and operations on it, `repodb.apply` changes
across many repos, and `repodb.cli` the command-line interface. Library
functions return data and raise exceptions; progress is logged to the
``repodb`` logger, which is silent unless configured.
"""

import logging
from importlib.metadata import PackageNotFoundError, version

from repodb.apply import Repo, Run, apply_run, publish_run, runs_dir
from repodb.cli import main
from repodb.core import (
    DB_PATH,
    Change,
    Difference,
    GitRepoDB,
    Refresh,
    clone,
    export_projects,
    github,
    host_of,
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

logging.getLogger("repodb").addHandler(logging.NullHandler())

__all__ = [
    "DB_PATH",
    "Change",
    "Difference",
    "GitRepoDB",
    "Refresh",
    "Repo",
    "Run",
    "apply_run",
    "clone",
    "export_projects",
    "github",
    "host_of",
    "import_projects",
    "info",
    "main",
    "owner_of",
    "publish_run",
    "read_projects",
    "refresh",
    "repo_of",
    "runs_dir",
    "scan",
    "status",
    "to_json",
]
try:
    __version__ = version("repodb")
except PackageNotFoundError:  # run from a source tree without installing
    __version__ = "unknown"
