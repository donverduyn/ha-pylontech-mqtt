#!/bin/bash
# Keep exactly one eligible Dependabot group PR in GitHub auto-merge. When it
# merges, the resulting default-branch push invokes the workflow again and the
# next group becomes active against the new base.
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN must be set}"
: "${REPO:?REPO must be set}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

prs="$(gh pr list --repo "$REPO" --state open --limit 1000 \
  --json number,author,url,title,body,createdAt,autoMergeRequest \
  --jq '[.[] | select(.author.login == "dependabot[bot]" or .author.login == "app/dependabot")] | sort_by(.createdAt)')"

if [ "$(jq 'length' <<<"$prs")" = "0" ]; then
  echo "No open Dependabot PRs."
  exit 0
fi

# Start from a known single-flight state. This also migrates PRs that had
# auto-merge enabled by the previous all-Dependabot implementation.
while read -r pr; do
  number="$(jq -r .number <<<"$pr")"
  auto_merge="$(jq -r '.autoMergeRequest != null' <<<"$pr")"
  if [ "$auto_merge" = "true" ]; then
    echo "PR #$number: disabling existing auto-merge before queue selection."
    gh pr merge "$number" --repo "$REPO" --disable-auto \
      || echo "  could not disable auto-merge; PR may have changed concurrently."
  fi
done < <(jq -c '.[]' <<<"$prs")

selected=""
while read -r pr; do
  number="$(jq -r .number <<<"$pr")"
  title="$(jq -r .title <<<"$pr")"
  body="$(jq -r .body <<<"$pr")"

  pr_kind="$("$script_dir"/dependabot-pr-kind.sh "$body")"
  if [ "$pr_kind" != "group" ]; then
    echo "PR #$number: single-dependency update — keeping current, never queueing."
    continue
  fi

  bump_kind="$("$script_dir"/dependabot-bump-kind.sh "$title" "$body")"
  if [ "$bump_kind" != "minor-or-patch" ]; then
    echo "PR #$number: grouped $bump_kind update — leaving for manual review."
    continue
  fi

  # Failed required checks should not hold every later ecosystem update in
  # the queue. A pending PR remains eligible: auto-merge will simply wait.
  checks="$(gh pr checks "$number" --repo "$REPO" --required --json bucket 2>/dev/null || true)"
  if [ -n "$checks" ] \
     && jq -e '[.[] | select(.bucket == "fail" or .bucket == "cancel")] | length > 0' \
       >/dev/null <<<"$checks"; then
    echo "PR #$number: required checks failed or were cancelled — skipping this queue pass."
    continue
  fi

  selected="$pr"
  break
done < <(jq -c '.[]' <<<"$prs")

if [ -z "$selected" ]; then
  echo "No eligible Dependabot group PR is ready for auto-merge."
  exit 0
fi

number="$(jq -r .number <<<"$selected")"
url="$(jq -r .url <<<"$selected")"
echo "PR #$number: selected as the only active Dependabot group update."

updated=0
if output="$(gh pr update-branch "$number" --repo "$REPO" --rebase 2>&1)"; then
  updated=1
  echo "$output"
else
  echo "$output"
fi

if [ "$updated" != "1" ]; then
  merge_state="$(gh pr view "$number" --repo "$REPO" --json mergeStateStatus --jq .mergeStateStatus)"
  if [ "$merge_state" = "BEHIND" ] || [ "$merge_state" = "DIRTY" ] || [ "$merge_state" = "DRAFT" ]; then
    echo "PR #$number: branch state is $merge_state and automatic update failed — not enabling auto-merge."
    exit 0
  fi
fi

gh pr merge "$url" --repo "$REPO" --auto --squash
echo "PR #$number: auto-merge enabled; later grouped updates remain disabled."
