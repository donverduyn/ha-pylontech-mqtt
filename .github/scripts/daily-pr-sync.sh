#!/bin/bash
# Runs once daily against every open PR, oldest first, one PR fully handled
# before the next is even looked at. For each non-draft PR:
#   1. Rebase it onto the current base branch. Always -- Dependabot and
#      human PRs alike, including ones that can never be merged
#      automatically -- so every branch stays as fresh as automation can
#      make it, even a PR stuck failing on its own merits.
#   2. If (and only if) it's a Dependabot grouped patch/minor update, wait
#      for its required checks to finish on that freshly rebased commit,
#      then merge it for real (not gh's async --auto) if they pass.
#
# Merging for real before moving to the next PR -- rather than enabling
# --auto and moving on -- is deliberate: it's what lets the next PR's
# rebase in step 1 actually pick up this run's earlier merges, instead of
# racing GitHub's own async completion. Individual/major Dependabot updates
# and every human PR only ever get rebased; this workflow never merges a
# human PR, and never touches a human PR's own auto-merge choice.
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN must be set}"
: "${REPO:?REPO must be set}"

# How long to wait for a single rebased Dependabot PR's required checks to
# reach a final state before giving up on it for today (it stays rebased
# either way, and gets re-attempted on the next daily run).
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-30}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-600}"

# Overall per-run budget for check-waiting, independent of the above. This
# wait can repeat once per eligible PR with nothing else capping how many
# eligible PRs appear in one run, so without a run-level budget too, several
# eligible PRs landing the same day could sum past both the job's
# timeout-minutes and the ~1-hour App installation token lifetime -- and
# GitHub would just hard-kill the job mid-poll with no clean stopping point.
# Kept comfortably under both: once spent, later PRs are left rebased (not
# merged) for today, deterministically and visibly, rather than relying on
# a runner-imposed cancellation to cut things off.
RUN_BUDGET_SECONDS="${RUN_BUDGET_SECONDS:-2700}"
run_deadline="$(( $(date +%s) + RUN_BUDGET_SECONDS ))"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

is_dependabot() {
  [ "$1" = "dependabot[bot]" ] || [ "$1" = "app/dependabot" ]
}

# `gh pr checks` reports a PR-level rollup that can lag behind a rebase: it
# was observed reporting a fresh rebase's required checks as already
# "pass" within ~1.5s of the push -- almost certainly still the previous
# commit's already-resolved state, not the new commit's -- and the
# resulting merge attempt was then rejected by branch protection because
# the real, current-commit check state hadn't reached "pass" yet. Querying
# check-runs scoped to the exact head SHA instead removes that ambiguity
# by construction: results can never belong to any commit but this one.
# Required context names are read once from branch protection itself
# rather than hardcoded, so this keeps working if they ever change.
default_branch="$(gh repo view "$REPO" --json defaultBranchRef --jq .defaultBranchRef.name)"
required_contexts="$(gh api "repos/$REPO/branches/$default_branch/protection/required_status_checks/contexts" 2>/dev/null || echo '[]')"

# Echoes exactly one of: pass | fail | timeout
wait_for_required_checks() {
  local number="$1" sha="$2"
  local deadline runs relevant total_required
  deadline="$(( $(date +%s) + MAX_WAIT_SECONDS ))"
  total_required="$(jq 'length' <<<"$required_contexts")"

  if [ "$total_required" = "0" ]; then
    echo "pass"
    return
  fi

  while true; do
    if runs="$(gh api "repos/$REPO/commits/$sha/check-runs" --jq '[.check_runs[] | {name, status, conclusion}]' 2>&1)"; then
      relevant="$(jq --argjson ctx "$required_contexts" \
        '[.[] | select(.name as $n | $ctx | index($n) != null)]' <<<"$runs")"

      if [ "$(jq 'length' <<<"$relevant")" = "$total_required" ] \
        && jq -e 'all(.status == "completed")' >/dev/null <<<"$relevant"; then
        if jq -e '[.[] | select(.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped")] | length > 0' \
          >/dev/null <<<"$relevant"; then
          echo "fail"
        else
          echo "pass"
        fi
        return
      fi
    else
      # A real failure (rate limit, transient API error, an expiring token)
      # looks identical to "still running" unless logged separately --
      # surfaced on stderr so it doesn't corrupt this function's
      # pass/fail/timeout return value on stdout.
      echo "PR #$number: gh api check-runs failed (${runs}) -- treating as not yet complete." >&2
    fi

    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "timeout"
      return
    fi

    sleep "$POLL_INTERVAL_SECONDS"
  done
}

prs="$(gh pr list --repo "$REPO" --state open --limit 1000 \
  --json number,author,url,title,body,isDraft,createdAt,autoMergeRequest \
  --jq 'sort_by(.createdAt)')"

if [ "$(jq 'length' <<<"$prs")" = "0" ]; then
  echo "No open PRs."
  exit 0
fi

while read -r pr; do
  number="$(jq -r .number <<<"$pr")"
  url="$(jq -r .url <<<"$pr")"
  author="$(jq -r '.author.login' <<<"$pr")"
  title="$(jq -r .title <<<"$pr")"
  body="$(jq -r .body <<<"$pr")"
  is_draft="$(jq -r .isDraft <<<"$pr")"

  if [ "$is_draft" = "true" ]; then
    echo "PR #$number: draft — skipping."
    continue
  fi

  # Clean up any auto-merge this workflow's older async design could have
  # left enabled. This workflow merges synchronously now, so a leftover
  # --auto flag could otherwise complete an out-of-sequence merge on its
  # own. Only ever touches auto-merge on a Dependabot PR -- never a
  # human's own choice.
  if is_dependabot "$author" \
    && [ "$(jq -r '.autoMergeRequest != null' <<<"$pr")" = "true" ]; then
    echo "PR #$number: disabling stale auto-merge."
    gh pr merge "$number" --repo "$REPO" --disable-auto \
      || echo "PR #$number: could not disable auto-merge; PR may have changed concurrently."
  fi

  echo "PR #$number: rebasing onto current base."
  if ! gh pr update-branch "$number" --repo "$REPO" --rebase 2>&1; then
    echo "PR #$number: rebase not applied (already current, conflicted, or not permitted)."
  fi

  if ! is_dependabot "$author"; then
    continue
  fi

  pr_kind="$("$script_dir"/dependabot-pr-kind.sh "$body")"
  bump_kind="$("$script_dir"/dependabot-bump-kind.sh "$title" "$body")"
  if [ "$pr_kind" != "group" ] || [ "$bump_kind" != "minor-or-patch" ]; then
    echo "PR #$number: $pr_kind/$bump_kind Dependabot update — rebased only, never merged here."
    continue
  fi

  if ! merge_state="$(gh pr view "$number" --repo "$REPO" --json mergeStateStatus --jq .mergeStateStatus 2>&1)"; then
    echo "PR #$number: could not read merge state (${merge_state}) — leaving for the next run."
    continue
  fi
  if [ "$merge_state" = "DIRTY" ] || [ "$merge_state" = "BEHIND" ]; then
    echo "PR #$number: branch state is $merge_state after rebase — leaving for the next run."
    continue
  fi

  if [ "$(date +%s)" -ge "$run_deadline" ]; then
    echo "PR #$number: this run's check-wait budget is spent — leaving for the next run."
    continue
  fi

  if ! head_sha="$(gh pr view "$number" --repo "$REPO" --json headRefOid --jq .headRefOid 2>&1)"; then
    echo "PR #$number: could not read head commit (${head_sha}) — leaving for the next run."
    continue
  fi

  echo "PR #$number: waiting for required checks on commit $head_sha."
  case "$(wait_for_required_checks "$number" "$head_sha")" in
    pass)
      echo "PR #$number: required checks passed — merging now."
      gh pr merge "$url" --repo "$REPO" --squash \
        || echo "PR #$number: merge attempt was rejected — leaving for the next run."
      ;;
    fail)
      echo "PR #$number: required checks failed — leaving for the next run."
      ;;
    timeout)
      echo "PR #$number: required checks still pending after ${MAX_WAIT_SECONDS}s — leaving for the next run."
      ;;
  esac
done < <(jq -c '.[]' <<<"$prs")
