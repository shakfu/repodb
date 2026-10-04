#!/bin/sh
# A change for: repodb apply RUN --script THIS_FILE -m MESSAGE [selection]
#
# Runs once per repo, in the root of a fresh clone of its default branch.
# Exit 0 after changing files: repodb commits them.
# Exit 0 without changes: the repo is "unchanged".
# Exit non-zero: the repo is "failed".
# Do not commit, push or switch branches; repodb does that.
# Leave no temporary files: repodb commits every new file.
#
# Environment: REPODB_RUN, REPODB_OWNER, REPODB_NAME, REPODB_URL.
# Output goes to the repo's log, runs/RUN/logs/OWNER__NAME.log.
set -eu

# Skip repos the change does not apply to.
# [ -f package.json ] || exit 0

# Make the change. Example: replace a string in tracked Markdown files.
# git grep -lz 'old-name' -- '*.md' | xargs -0 perl -pi -e 's/old-name/new-name/g'

echo "$0: replace this line with the change" >&2
exit 1
