#!/usr/bin/env bash
# Smoke test of repodb against real GitHub: PR-mode publish, its failure paths,
# the workflow-scope check, and refresh --prune. The test suite mocks gh; this
# does not.
#
# Uses two private repos under the gh account, SMOKE_A and SMOKE_B (default
# scratch-repodb-a, scratch-repodb-b), and creates them if missing. They are
# kept between runs, so no delete_repo scope is needed. Each run's branches end
# in its timestamp; at exit their PRs are closed, the branches deleted, and
# SMOKE_B's rename undone. Default branches are not changed.
#
# Git uses gh for HTTPS credentials in this script only; your git config is not
# changed.
#
# Usage: scripts/smoke-github.sh
# A failed check is reported and the next case runs; the exit status is the
# number of failed checks.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
DB=$WORK/state/repos.sqlite
SHIM=$WORK/shim
REAL_GH=$(command -v gh)
OWNER=$(gh api user --jq .login) || { echo "gh is not logged in"; exit 2; }
A=${SMOKE_A:-scratch-repodb-a}
B=${SMOKE_B:-scratch-repodb-b}
STAMP=$(date +%s)
failures=0

# What `gh auth setup-git` writes, for this process only: clear other helpers
# for github.com, then use gh.
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=credential.https://github.com.helper GIT_CONFIG_VALUE_0=
export GIT_CONFIG_KEY_1=credential.https://github.com.helper
export GIT_CONFIG_VALUE_1="!$REAL_GH auth git-credential"

repodb() { uv run --quiet --project "$ROOT" repodb --db "$DB" "$@"; }
exists() { gh repo view "$OWNER/$1" >/dev/null 2>&1; }
# GitHub redirects a renamed repo's old name, so `exists` alone cannot tell.
named() { [ "$(gh repo view "$OWNER/$1" --json name --jq .name 2>/dev/null)" = "$1" ]; }

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; failures=$((failures + 1)); }
check() {  # check DESCRIPTION ACTUAL EXPECTED
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1: got '$2', want '$3'"; fi
}

# field RUN KEY: a value of $OWNER/$A from runs/RUN/run.json
field() {
    python3 -c 'import json,sys; r=json.load(open(sys.argv[1]))["repos"][sys.argv[2]]
v=r.get(sys.argv[3]); print("" if v is None else v)' \
        "$WORK/state/runs/$1/run.json" "$OWNER/$A" "$2" 2>/dev/null
}
remote_head() { git ls-remote "https://github.com/$OWNER/$A.git" "refs/heads/$1" | cut -f1; }

cleanup() {
    echo "== cleanup"
    if named "$B-renamed"; then
        gh repo rename "$B" -R "$OWNER/$B-renamed" --yes >/dev/null \
            || echo "rename back by hand: gh repo rename $B -R $OWNER/$B-renamed --yes"
    fi
    for n in $(gh pr list -R "$OWNER/$A" --state open --limit 100 \
        --json number,headRefName --jq ".[] | select(.headRefName | endswith(\"-$STAMP\")) | .number"); do
        gh pr close "$n" -R "$OWNER/$A" >/dev/null || echo "close by hand: PR $n"
    done
    for ref in $(git ls-remote "https://github.com/$OWNER/$A.git" "refs/heads/repodb/*-$STAMP" | cut -f2); do
        gh api -X DELETE "repos/$OWNER/$A/git/$ref" >/dev/null \
            || echo "delete by hand: branch ${ref#refs/heads/}"
    done
    rm -rf "$WORK"
}

echo "== setup: $OWNER/$A, $OWNER/$B, runs *-$STAMP"
# Undo a rename left by an interrupted run.
if named "$B-renamed"; then
    gh repo rename "$B" -R "$OWNER/$B-renamed" --yes >/dev/null || exit 2
fi
for r in "$A" "$B"; do
    exists "$r" || gh repo create "$OWNER/$r" --private --add-readme >/dev/null || exit 2
done
sleep 3  # let a new repo's default branch appear
trap cleanup EXIT
git ls-remote "https://github.com/$OWNER/$A.git" >/dev/null \
    || { echo "HTTPS git access to $OWNER/$A failed"; exit 2; }

# gh stand-in: SMOKE_GH=fail makes `pr create` fail and `pr list` find nothing;
# SMOKE_GH=lost opens the PR but reports failure, as after a lost response.
mkdir -p "$SHIM"
cat >"$SHIM/gh" <<EOF
#!/bin/sh
case "\${SMOKE_GH:-}:\$1 \$2" in
  "fail:pr create") echo "smoke: forced failure" >&2; exit 1 ;;
  "fail:pr list") exit 0 ;;
  "lost:pr create") "$REAL_GH" "\$@" >/dev/null; exit 1 ;;
esac
exec "$REAL_GH" "\$@"
EOF
chmod +x "$SHIM/gh"
shimmed() { PATH="$SHIM:$PATH" SMOKE_GH=$1 repodb "${@:2}"; }

printf '{"%s": "https://github.com/%s/%s.git", "%s": "https://github.com/%s/%s.git"}\n' \
    "$A" "$OWNER" "$A" "$B" "$OWNER" "$B" >"$WORK/projects.json"
repodb import "$WORK/projects.json" >/dev/null || exit 2
repodb set --name t "$A" >/dev/null || exit 2

R1=t1-$STAMP R2=t2-$STAMP R3=t3-$STAMP R4=t4-$STAMP R5=t5-$STAMP

echo "== 1. publish opens a draft PR"
repodb apply "$R1" -s t --exec 'echo one >> README.md' -m "Smoke 1"
repodb publish "$R1" --draft
check "1 state" "$(field "$R1" state)" published
pr=$(field "$R1" pr)
check "1 PR is draft" "$(gh pr view "$pr" --json isDraft --jq .isDraft 2>/dev/null)" true

echo "== 2. redo after the branch was pushed and gh pr create failed"
repodb apply "$R2" -s t --exec 'echo two >> README.md' -m "Smoke 2"
shimmed fail publish "$R2"
check "2 state after failed PR" "$(field "$R2" state)" committed
check "2 branch pushed" "$(remote_head "repodb/$R2")" "$(field "$R2" commit)"
repodb apply "$R2" -m "Smoke 2 redone" --redo "$A"
repodb publish "$R2"
check "2 state after redo" "$(field "$R2" state)" published
check "2 branch is the redone commit" "$(remote_head "repodb/$R2")" "$(field "$R2" commit)"

echo "== 3. the lease keeps a commit someone else added to the branch"
repodb apply "$R3" -s t --exec 'echo three >> README.md' -m "Smoke 3"
shimmed fail publish "$R3"
other=$WORK/other
git clone -q -b "repodb/$R3" "https://github.com/$OWNER/$A.git" "$other"
echo theirs >>"$other/README.md"
git -C "$other" -c user.name=smoke -c user.email=smoke@example.com commit -qam theirs
git -C "$other" push -q
theirs=$(git -C "$other" rev-parse HEAD)
repodb apply "$R3" -m "Smoke 3 redone" --redo "$A"
repodb publish "$R3"
check "3 state" "$(field "$R3" state)" committed
check "3 branch unchanged" "$(remote_head "repodb/$R3")" "$theirs"

echo "== 4. a PR opened but not recorded is found on retry"
repodb apply "$R4" -s t --exec 'echo four >> README.md' -m "Smoke 4"
shimmed lost publish "$R4"
check "4 state" "$(field "$R4" state)" published
case "$(field "$R4" pr)" in
    https://github.com/*/pull/*) pass "4 PR recorded" ;;
    *) fail "4 PR recorded: got '$(field "$R4" pr)'" ;;
esac

echo "== 5. workflow-scope check"
wf='mkdir -p .github/workflows && printf "on: workflow_dispatch\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n" > .github/workflows/smoke.yml'
repodb apply "$R5" -s t --exec "$wf" -m "Smoke 5"
if repodb publish "$R5" 2>&1 | tee "$WORK/t5.out"; then
    check "5 state (token has the workflow scope)" "$(field "$R5" state)" published
elif grep -q "gh auth refresh -s workflow" "$WORK/t5.out"; then
    pass "5 refused: token lacks the workflow scope"
    check "5 branch not pushed" "$(remote_head "repodb/$R5")" ""
else
    fail "5: unexpected output"
fi

echo "== 6. refresh --prune reports a renamed repo, and does not prune it"
gh repo rename "$B-renamed" -R "$OWNER/$B" --yes >/dev/null
sleep 3
repodb github --refresh "$OWNER" --prune 2>&1 | tee "$WORK/prune.out"
if grep -q "$OWNER/$B (renamed to $OWNER/$B-renamed)" "$WORK/prune.out"; then
    pass "6 rename reported"
else
    fail "6 rename reported"
fi
if grep -q "would remove: $OWNER/$B " "$WORK/prune.out"; then
    fail "6 renamed repo listed for removal"
else
    pass "6 renamed repo not listed for removal"
fi

echo
if [ "$failures" -eq 0 ]; then echo "all passed"; else echo "$failures failed"; fi
exit "$failures"
