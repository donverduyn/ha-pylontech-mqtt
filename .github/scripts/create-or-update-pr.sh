#!/bin/bash
# Commits working-tree changes to a dedicated branch, pushes it with the
# short-lived token the caller supplied, and creates or updates the PR.
#
# Invoked by .github/actions/create-or-update-pr, which is the thing
# dependency-updates.yaml and min-ha-version-update.yaml actually reference.
# It lives here rather than inline in that action's `run:` block so it gets
# linted: actionlint does not lint composite actions at all, so shell written
# inside one receives neither actionlint's own checks nor the shellcheck pass
# it embeds for workflow `run:` blocks. meta-lint's ShellCheck step discovers
# every *.sh file via `git ls-files`, so a real script file is covered
# automatically (see tests.yaml).
#
# Reads its inputs from the environment, set by the composite action from its
# own `inputs:`; writes `changed` to $GITHUB_OUTPUT.
#
# Runs with the repository as the working directory (composite `run:` steps
# execute in $GITHUB_WORKSPACE), which is what the git commands below assume.
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN must be set}"
: "${APP_SLUG:?APP_SLUG must be set}"
: "${BRANCH:?BRANCH must be set}"
: "${BASE_BRANCH:?BASE_BRANCH must be set}"
: "${COMMIT_MESSAGE:?COMMIT_MESSAGE must be set}"
: "${PR_TITLE:?PR_TITLE must be set}"
: "${PR_BODY:?PR_BODY must be set}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT must be set}"

if [ -z "$(git status --porcelain)" ]; then
  echo "No changes found."
  echo "changed=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

app_user_id="$(gh api "/users/${APP_SLUG}[bot]" --jq .id)"
git config user.name "${APP_SLUG}[bot]"
git config user.email "${app_user_id}+${APP_SLUG}[bot]@users.noreply.github.com"

# checkout uses persist-credentials:false. Configure gh's credential helper
# only in this final publication step, after dependency tools have finished,
# so the write token is not persisted during them.
gh auth setup-git

git checkout -B "$BRANCH"
git add --all
git commit -m "$COMMIT_MESSAGE"

# Lease against this branch as it exists *now*, not as actions/checkout
# recorded it minutes ago. A bare --force-with-lease leases against the
# checkout-time remote-tracking ref, and daily-pr-sync.sh rebases every open PR
# -- including this branch's own -- on its daily run, which on Mondays starts
# five minutes after this workflow's. That rebase broke the stale lease and
# failed the push outright, even though the commit being pushed is wholly
# derived from this run's checkout of the base branch plus `make update-deps`:
# there is nothing on the remote branch worth preserving. Re-fetching narrows
# the lease to what it should have been all along -- a genuine concurrent push
# landing inside this fetch-to-push window -- instead of rejecting a rebase
# that already happened and does not matter.
#
# An empty <expect> ("$BRANCH:") is git's spelling of "this ref must not exist
# yet", which is the correct lease the first time a branch is published, and is
# what a failed fetch means here.
if git fetch --no-tags origin "$BRANCH" 2>/dev/null; then
  lease="$BRANCH:$(git rev-parse FETCH_HEAD)"
else
  lease="$BRANCH:"
fi
git push --force-with-lease="$lease" origin "$BRANCH"

pr_number="$(
  gh pr list --head "$BRANCH" --state open \
    --json number --jq '.[0].number // ""'
)"
if [ -n "$pr_number" ]; then
  gh pr edit "$pr_number" --title "$PR_TITLE" --body "$PR_BODY"
else
  gh pr create \
    --base "$BASE_BRANCH" \
    --head "$BRANCH" \
    --title "$PR_TITLE" \
    --body "$PR_BODY"
fi

echo "changed=true" >> "$GITHUB_OUTPUT"
