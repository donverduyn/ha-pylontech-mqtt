#!/bin/bash
# Runs once daily against every open PR, oldest first, one PR fully handled
# before the next is even looked at. For each non-draft PR:
#   1. Rebase it onto the current base branch. Always -- Dependabot and
#      human PRs alike, including ones that can never be merged
#      automatically -- so every branch stays as fresh as automation can
#      make it, even a PR stuck failing on its own merits.
#   2. Any Dependabot update Dependabot didn't group as a minor/patch
#      update -- an individual PR, or a major-version bump either way --
#      gets checked for supersession first (see below) and closed if
#      already met on the base branch. If it's not superseded and not a
#      major bump, it's treated the same as a grouped minor/patch update
#      from here: wait for its required checks on the freshly rebased
#      commit, then merge it for real (not gh's async --auto) if they
#      pass. Grouping is Dependabot's own ecosystem-level packaging
#      choice, not a safety signal -- only a real major-version bump
#      still gets left for a human to decide on.
#
# Merging for real before moving to the next PR -- rather than enabling
# --auto and moving on -- is deliberate: it's what lets the next PR's
# rebase in step 1 actually pick up this run's earlier merges, instead of
# racing GitHub's own async completion. Individual/major Dependabot updates
# and every human PR only ever get rebased; this workflow never merges a
# human PR, and never touches a human PR's own auto-merge choice.
#
# Why individual/major Dependabot PRs get a supersession check: a plain
# `gh pr update-branch --rebase` is pure git plumbing -- it patch-applies
# the PR's existing diff onto the new base and knows nothing about
# Dependabot. It was observed leaving PRs proposing a *lower* version than
# what a separate, Dependabot-blind refresh (dependency-updates.yaml's
# `make update-deps`) had already put on the base branch: the patch still
# applied cleanly (mergeStateStatus MERGEABLE), so nothing here ever
# flagged them, and since this workflow never merges individual/major
# Dependabot updates, they just sat open indefinitely.
#
# An earlier version of this asked Dependabot itself via an "@dependabot
# rebase" PR comment, on the theory that Dependabot would re-verify the
# update and close it itself if no longer needed -- deliberately avoiding
# ecosystem-specific version comparison here. That turned out to be
# unusable from automation: Dependabot's command handler checks the
# commenter's repository *collaborator* permission, and a GitHub App's bot
# identity is never a collaborator (a different permission system from
# the App's own installation permissions), so the command was always
# rejected with "only users with push access can use that command."
#
# dependabot-supersession-check.sh does the comparison directly instead,
# reading the currently-pinned version from one exact snapshot of the current
# default-branch commit, resolved immediately before each decision, rather than
# trusting either Dependabot or the job's now-stale initial checkout. Closing a
# confirmed-superseded PR only tells GitHub not to recreate *that exact* stale
# version pair; Dependabot remains free to open a fresh PR the next time a
# genuinely newer version appears.
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN must be set}"
: "${REPO:?REPO must be set}"

# How long to wait for a single rebased Dependabot PR's required checks to
# reach a final state before giving up on it for today (it stays rebased
# either way, and gets re-attempted on the next daily run).
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-30}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-600}"

# How long to let GitHub catch up with a rebase this script just requested
# before reading the PR's post-rebase state. Separate from, and much shorter
# than, the check wait above: this only covers GitHub's own asynchronous
# processing of the update-branch request, not any CI.
REBASE_SETTLE_SECONDS="${REBASE_SETTLE_SECONDS:-120}"
REBASE_SETTLE_INTERVAL_SECONDS="${REBASE_SETTLE_INTERVAL_SECONDS:-5}"

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

# Materialize the PR's changed files from one exact, freshly-read default-
# branch commit. The working tree is only the checkout from the start of this
# run; after this script merges an earlier PR it is stale, so reading it for a
# later supersession decision can miss the version that just landed.
# Echoes the temporary snapshot directory, or returns non-zero. Callers must
# remove a successful snapshot.
snapshot_default_branch_files() {
  local changed_files="$1"
  local base_sha snapshot file target_dir

  if ! base_sha="$(
    gh api "repos/$REPO/commits/$default_branch" --jq .sha 2>/dev/null
  )" || [ -z "$base_sha" ]; then
    echo "Could not resolve the current $default_branch commit for a supersession check." >&2
    return 1
  fi

  snapshot="$(mktemp -d)" || return 1
  while IFS= read -r file; do
    [ -z "$file" ] && continue
    case "$file" in
      /*|../*|*/../*|*/..)
        echo "Refusing unsafe changed-file path '$file'." >&2
        rm -r -- "$snapshot"
        return 1
        ;;
    esac

    target_dir="$snapshot/$(dirname -- "$file")"
    if ! mkdir -p -- "$target_dir" \
      || ! gh api "repos/$REPO/contents/$file?ref=$base_sha" \
        -H "Accept: application/vnd.github.raw+json" \
        >"$snapshot/$file"; then
      echo "Could not read '$file' from $default_branch at $base_sha." >&2
      rm -r -- "$snapshot"
      return 1
    fi
  done <<<"$changed_files"

  printf '%s\n' "$snapshot"
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

# Reading branch protection needs the automation App's "Administration: read"
# permission; contents/pull-requests/workflows alone get a 403. `gh api` writes
# an error response's *body* to stdout while exiting non-zero, so the previous
# `2>/dev/null || echo '[]'` fallback silently produced one variable holding
# two concatenated JSON documents. Both readings of that value were observed in
# production: `jq 'length'` returned "3\n0" (never "0", so the
# no-required-contexts shortcut never fired) and `--argjson ctx` died with
# "invalid JSON text", which before #163 became a false "pass" that merged
# seconds after a rebase and after #163 made every eligible PR spin to its
# timeout -- nothing could merge either way. There is no safe fallback here:
# an empty list means "merge without checking anything". Fail closed instead.
if ! required_contexts="$(
  gh api "repos/$REPO/branches/$default_branch/protection/required_status_checks/contexts"
)" || ! jq -e 'type == "array" and length > 0' >/dev/null 2>&1 <<<"$required_contexts"; then
  echo "::error::Could not read $default_branch's required status check contexts. The automation App needs the 'Administration: read' repository permission (see .github/AUTOMATION_APP.md); refusing to merge anything without knowing which checks are required."
  exit 1
fi

# Branch protection's list is the only copy that decides whether a PR is
# actually blocked, and it lives solely in GitHub's UI -- meta-lint's
# check_required_contexts.py can only check the two committed transcriptions
# (its own REQUIRED_CONTEXTS map and WORKFLOW_SECURITY.md) against the
# workflow files, never against this. This run already holds the list and the
# Administration: read grant needed to fetch it, so it is the one place the
# loop can be closed. A warning rather than a hard failure: an intentional
# protection edit should not stop the day's maintenance, it should just not
# pass unnoticed -- the committed copies are what a later reader trusts.
expected_contexts="$(python3 "$script_dir/check_required_contexts.py" --list | sort || true)"
actual_contexts="$(jq -r '.[]' <<<"$required_contexts" | sort)"
if [ -n "$expected_contexts" ] && [ "$expected_contexts" != "$actual_contexts" ]; then
  echo "::warning::$default_branch's required status checks have drifted from the committed list. Branch protection requires: $(tr '\n' ' ' <<<"$actual_contexts"). check_required_contexts.py expects: $(tr '\n' ' ' <<<"$expected_contexts"). Update REQUIRED_CONTEXTS and .github/WORKFLOW_SECURITY.md, or the protection rule."
fi

# Echoes exactly one of: pass | fail | timeout
wait_for_required_checks() {
  local number="$1" sha="$2"
  local deadline runs classification now remaining sleep_for
  deadline="$(( $(date +%s) + MAX_WAIT_SECONDS ))"
  # The per-PR ceiling must not extend the run-level ceiling. Without this
  # clamp, a wait started one second before run_deadline could consume another
  # full MAX_WAIT_SECONDS and collide with the job's 55-minute hard timeout.
  if [ "$deadline" -gt "$run_deadline" ]; then
    deadline="$run_deadline"
  fi

  # No "$required_contexts is empty" shortcut here on purpose: an empty list
  # would mean "merge without verifying anything", and the only way to get one
  # is a failed or malformed lookup, which is now rejected at startup instead.
  while true; do
    # Both the API call succeeding AND its output actually being one valid
    # JSON array are required before trusting it. Output that fails to parse
    # ("jq: invalid JSON text passed to --argjson") previously reached the
    # classification step anyway, whose failure was then silently swallowed
    # by the `!` below and misread as "no pending entries found", i.e. a
    # false "pass" -- exactly the untested-commit risk this whole function
    # exists to prevent. Every failure path here now falls through to the
    # same safe "not yet complete" branch instead.
    # --paginate, not a single default-page request: this endpoint returns 30
    # check-runs per page, and one commit here already carries ~17 across
    # tests.yaml/hacs.yaml/hassfest.yaml (three of which are the same
    # "detect-release / detect-release" name). A single re-run of Tests adds
    # eleven more, so two re-runs push the required contexts off page one --
    # they would then match nothing, classify as "pending", and every eligible
    # PR would spin to MAX_WAIT_SECONDS and never merge, with no error
    # anywhere. --jq '.check_runs[]' (rather than a `[...]`-wrapped filter)
    # emits one object per line across all pages for `jq -s` to slurp into a
    # single array; a wrapped filter would print one array per page as
    # separate top-level JSON values instead. Same shape tests.yaml's
    # codeql-gate uses, and for the same reason.
    if runs="$(
      gh api "repos/$REPO/commits/$sha/check-runs?per_page=100" --paginate \
        --jq '.check_runs[] | {name, status, conclusion}' 2>/dev/null \
        | jq -s '.'
    )" && jq -e 'type == "array"' >/dev/null 2>&1 <<<"$runs"; then
      # A required context can have more than one check-run entry for the
      # same commit (re-runs, multiple triggering events observed on this
      # repo's own "tests-finished" context) -- classify each required
      # name by whether any of its runs completed successfully, not by
      # counting total entries against the number of required names, or
      # duplicates make this loop until timeout even once everything has
      # actually passed.
      if classification="$(jq -n --argjson ctx "$required_contexts" --argjson runs "$runs" '
        $ctx | map(. as $name |
          ($runs | map(select(.name == $name))) as $matches |
          if ($matches | any(.status == "completed" and (.conclusion == "success" or .conclusion == "neutral" or .conclusion == "skipped")))
          then "pass"
          elif ($matches | length) > 0 and ($matches | all(.status == "completed"))
          then "fail"
          else "pending"
          end
        )' 2>/dev/null)"; then
        if ! jq -e 'any(. == "pending")' >/dev/null 2>&1 <<<"$classification"; then
          if jq -e 'any(. == "fail")' >/dev/null 2>&1 <<<"$classification"; then
            echo "fail"
          else
            echo "pass"
          fi
          return
        fi
      else
        echo "PR #$number: classifying check-runs failed -- treating as not yet complete." >&2
      fi
    else
      # A real failure (rate limit, transient API error, an expiring
      # token) looks identical to "still running" unless logged
      # separately -- surfaced on stderr so it doesn't corrupt this
      # function's pass/fail/timeout return value on stdout.
      echo "PR #$number: gh api check-runs failed or returned unparseable output -- treating as not yet complete." >&2
    fi

    now="$(date +%s)"
    if [ "$now" -ge "$deadline" ]; then
      echo "timeout"
      return
    fi

    remaining="$(( deadline - now ))"
    sleep_for="$POLL_INTERVAL_SECONDS"
    if [ "$sleep_for" -gt "$remaining" ]; then
      sleep_for="$remaining"
    fi
    sleep "$sleep_for"
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


  # Dependabot grouping is an ecosystem-level packaging choice, not a
  # safety signal. An individual update or an action with no tagged releases
  # to compare is no riskier than a grouped update, but anything outside the
  # grouped-and-merged path still needs a supersession check so stale PRs do
  # not retry forever after the base branch has already overtaken them.
  if [ "$pr_kind" != "group" ] || [ "$bump_kind" != "minor-or-patch" ]; then
    changed_files="$(gh pr diff "$number" --repo "$REPO" --name-only 2>/dev/null || true)"
    if base_snapshot="$(snapshot_default_branch_files "$changed_files")"; then
      supersession="$(
        cd -- "$base_snapshot"
        "$script_dir"/dependabot-supersession-check.sh "$title" "$body" \
          <<<"$changed_files"
      )"
      rm -r -- "$base_snapshot"
    else
      supersession="unknown"
    fi
    if [ "$supersession" = "superseded" ]; then
      echo "PR #$number: $pr_kind/$bump_kind Dependabot update — proposed version is already superseded on $default_branch, closing."
      gh pr close "$number" --repo "$REPO" --comment \
        "Closing: the version this PR proposes is already met or exceeded on \`$default_branch\`. Dependabot will open a fresh PR if a newer update is still needed." \
        || echo "PR #$number: could not close; PR may have changed concurrently."
      continue
    fi

    # A real major-version bump is the one case that still warrants a
    # human looking at it first, even once it's confirmed not superseded.
    if [ "$bump_kind" = "major" ]; then
      echo "PR #$number: $pr_kind/$bump_kind Dependabot update — rebased only, never merged here."
      continue
    fi
  fi

  # `gh pr update-branch` returns as soon as GitHub accepts the request; the
  # rebase itself lands asynchronously. Both mergeStateStatus and headRefOid
  # can still describe the pre-rebase commit for a moment afterwards.
  # mergeStateStatus can even be a stale CLEAN immediately after an earlier PR
  # changed the base. Prove the current baseRefOid is actually an ancestor of
  # the candidate head instead of trusting that rollup alone; only then bind
  # the later check wait and merge to that exact head SHA.
  # Clamped to the run budget as well as its own: this wait can repeat once
  # per PR, so without the clamp a day with many PRs stuck BEHIND could spend
  # REBASE_SETTLE_SECONDS on each and push past the job's timeout-minutes,
  # which is the runner-imposed hard kill RUN_BUDGET_SECONDS exists to keep
  # this script clear of.
  settle_deadline="$(( $(date +%s) + REBASE_SETTLE_SECONDS ))"
  if [ "$settle_deadline" -gt "$run_deadline" ]; then
    settle_deadline="$run_deadline"
  fi
  branch_ready=false
  merge_state=UNKNOWN
  ancestry_status=unknown
  head_sha=""
  base_sha=""
  while true; do
    if ! merge_state="$(gh pr view "$number" --repo "$REPO" --json mergeStateStatus --jq .mergeStateStatus 2>&1)"; then
      echo "PR #$number: could not read merge state (${merge_state}) — leaving for the next run."
      # continue 2, not 1: skip to the next PR rather than re-polling this
      # one. Reaches the outer loop because it is a plain `while read ...
      # done < <(...)` in this same shell, not a pipeline subshell.
      continue 2
    fi

    if [ "$merge_state" = "DIRTY" ]; then
      break
    fi
    if [ "$merge_state" != "BEHIND" ] && [ "$merge_state" != "UNKNOWN" ]; then
      if ! head_sha="$(gh pr view "$number" --repo "$REPO" --json headRefOid --jq .headRefOid 2>&1)" \
        || ! base_sha="$(gh pr view "$number" --repo "$REPO" --json baseRefOid --jq .baseRefOid 2>&1)"; then
        echo "PR #$number: could not read head/base commits — leaving for the next run."
        continue 2
      fi
      if ancestry_status="$(
        gh api "repos/$REPO/compare/$base_sha...$head_sha" --jq .status 2>/dev/null
      )" && { [ "$ancestry_status" = "ahead" ] || [ "$ancestry_status" = "identical" ]; }; then
        branch_ready=true
        break
      fi
    fi

    if [ "$(date +%s)" -ge "$settle_deadline" ]; then
      break
    fi
    sleep "$REBASE_SETTLE_INTERVAL_SECONDS"
  done
  if [ "$branch_ready" != "true" ]; then
    echo "PR #$number: branch is not proven current after rebase (state: $merge_state, ancestry: $ancestry_status) — leaving for the next run."
    continue
  fi

  if [ "$(date +%s)" -ge "$run_deadline" ]; then
    echo "PR #$number: this run's check-wait budget is spent — leaving for the next run."
    continue
  fi

  echo "PR #$number: waiting for required checks on commit $head_sha."
  case "$(wait_for_required_checks "$number" "$head_sha")" in
    pass)
      # Re-read both refs after the check wait. A force-push must not swap in a
      # different, unchecked head, and a base change must not make this head
      # stale between the settle check and the merge attempt.
      if ! merge_head="$(gh pr view "$number" --repo "$REPO" --json headRefOid --jq .headRefOid 2>&1)" \
        || ! merge_base="$(gh pr view "$number" --repo "$REPO" --json baseRefOid --jq .baseRefOid 2>&1)" \
        || ! merge_state="$(gh pr view "$number" --repo "$REPO" --json mergeStateStatus --jq .mergeStateStatus 2>&1)"; then
        echo "PR #$number: could not re-read branch state after checks — leaving for the next run."
        continue
      fi
      if [ "$merge_head" != "$head_sha" ]; then
        echo "PR #$number: head changed from $head_sha to $merge_head after checks — leaving for the next run."
        continue
      fi
      if [ "$merge_state" = "DIRTY" ] || [ "$merge_state" = "BEHIND" ] \
        || [ "$merge_state" = "UNKNOWN" ]; then
        echo "PR #$number: branch state became $merge_state after checks — leaving for the next run."
        continue
      fi
      if ! merge_ancestry="$(
        gh api "repos/$REPO/compare/$merge_base...$merge_head" --jq .status 2>/dev/null
      )" || { [ "$merge_ancestry" != "ahead" ] && [ "$merge_ancestry" != "identical" ]; }; then
        echo "PR #$number: base is no longer an ancestor of the checked head — leaving for the next run."
        continue
      fi

      echo "PR #$number: required checks passed on the current head — merging now."
      # --delete-branch is belt-and-braces: the repository does have
      # "automatically delete head branches" enabled (it did not when this
      # merge path was first written, which is what the flag was added for),
      # so this normally deletes a branch GitHub was about to delete anyway.
      # Kept because it makes the cleanup this script's own doing rather than
      # a repository setting's, and costs nothing when the setting is on.
      gh pr merge "$url" --repo "$REPO" --match-head-commit "$head_sha" \
        --squash --delete-branch \
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
