#!/usr/bin/env python3
"""A change for: repodb apply RUN --script THIS_FILE -m MESSAGE [selection]

Runs once per repo, in the root of a fresh clone of its default branch.
Return 0 after changing files: repodb commits them.
Return 0 without changes: the repo is "unchanged".
Return non-zero: the repo is "failed".
Do not commit, push or switch branches; repodb does that.
Leave no temporary files: repodb commits every new file.

Environment: REPODB_RUN, REPODB_OWNER, REPODB_NAME, REPODB_URL.
Output goes to the repo's log, runs/RUN/logs/OWNER__NAME.log.
Standard library only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def substitute(path: Path, pattern: str, repl: str) -> int:
    """Apply `re.subn` to UTF-8 *path*, keeping line endings; return the count."""
    if not path.is_file():
        return 0
    new, n = re.subn(pattern, repl, path.read_bytes().decode("utf-8"))
    if n:
        path.write_bytes(new.encode("utf-8"))
    return n


def main() -> int:
    # Skip repos the change does not apply to.
    # if not Path("pyproject.toml").is_file():
    #     return 0

    # Make the change. Example:
    # substitute(Path("README.md"), r"old-name", "new-name")

    print(f"{sys.argv[0]}: replace this line with the change", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
