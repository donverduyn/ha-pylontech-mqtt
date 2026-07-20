#!/bin/bash
# Best-effort branch freshness for every open PR. Dependabot branches are
# rebased because they are disposable bot branches; contributor branches use
# GitHub's merge-based update so automation never rewrites human history.
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN must be set}"
: "${REPO:?REPO must be set}"

prs="$(gh pr list --repo "$REPO" --state open --limit 1000 \
  --json number,author,isCrossRepository,maintainerCanModify,createdAt \
  --jq 'sort_by(.createdAt)')"

if [ "$(jq 'length' <<<"$prs")" = "0" ]; then
  echo "No open PR branches to update."
  exit 0
fi

while read -r pr; do
  number="$(jq -r .number <<<"$pr")"
  author="$(jq -r '.author.login // ""' <<<"$pr")"
  is_fork="$(jq -r .isCrossRepository <<<"$pr")"
  maintainer_can_modify="$(jq -r .maintainerCanModify <<<"$pr")"

  update_args=("$number" --repo "$REPO")
  update_kind="merge update"
  if [ "$author" = "dependabot[bot]" ] || [ "$author" = "app/dependabot" ]; then
    update_args+=(--rebase)
    update_kind="rebase"
  fi

  echo "::group::PR #$number ($author): $update_kind"
  if [ "$is_fork" = "true" ] && [ "$maintainer_can_modify" != "true" ]; then
    echo "Cannot update: fork owner has not allowed maintainer modifications."
    echo "::endgroup::"
    continue
  fi

  # An already-current branch and a genuinely unmergeable branch both make
  # gh return non-zero. Keep this best-effort so one conflict never prevents
  # later PRs from being refreshed; the API message records which case it was.
  if output="$(gh pr update-branch "${update_args[@]}" 2>&1)"; then
    echo "$output"
  else
    echo "$output"
    echo "Branch was already current or could not be updated automatically."
  fi
  echo "::endgroup::"
done < <(jq -c '.[]' <<<"$prs")
