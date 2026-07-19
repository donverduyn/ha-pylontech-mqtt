#!/bin/bash
# Classifies a Dependabot PR's version bump as "major", "minor-or-patch", or
# "unknown". Shared by dependabot-auto-merge.yaml (decides whether to
# auto-merge) and close-stale-automation-prs.yaml (decides whether to exempt
# a PR from the generic staleness close) -- previously two independent,
# hand-copied ~20-line implementations of the same badge-parsing logic, with
# no title fallback, so any PR on an ecosystem that doesn't render the
# badge (see below) was permanently unclassifiable.
#
# Usage: dependabot-bump-kind.sh "$title" "$body"
# Echoes exactly one of: major | minor-or-patch | unknown
#
# Two independent signals, checked in order:
#
#  1. Per-dependency compatibility-score badges in the PR body
#     (a Markdown image link containing `previous-version=X&new-version=Y`
#     per dependency). Present for individually bumped dependencies on
#     registries with semver metadata (pip, npm), and still present
#     per-dependency inside a *grouped* multi-dependency PR body even
#     though the group itself has no single version of its own.
#
#  2. The PR title's own "bump NAME from OLD to NEW" text -- Dependabot's
#     standard title template for a single-dependency PR. This is the
#     ONLY signal available for ecosystems that never render the
#     compatibility badge at all -- confirmed empirically against this
#     repo's real Dependabot PRs: neither #119 (astral-sh/setup-uv, a
#     minor bump) nor #121 (softprops/action-gh-release, a patch bump)
#     carry any badge in their body, because github-actions and docker
#     aren't registries Dependabot's badge service covers. Without this
#     fallback, every github-actions/docker PR was unclassifiable and
#     fell to "unknown" unconditionally -- meaning auto-merge could never
#     apply to those ecosystems at all, regardless of how trivial the
#     bump. A grouped multi-dependency PR's title ("bump the X group with
#     N updates") doesn't match this pattern, so it naturally falls
#     through to signal 1 having already handled it (or to "unknown" if
#     signal 1 also found nothing, which is the correct conservative
#     outcome for a group with no per-dependency data at all).
#
# Only the leading digit run of each version is compared (matching the
# original badge-only logic) -- deliberately generic and ecosystem-
# agnostic rather than special-cased per registry, since `gh pr merge
# --auto` only *enables* auto-merge; it still requires every required
# status check (tests-finished/HACS Action/validate) to pass before
# anything actually merges, so this classification is a "does a human
# need to look at this" gate, not a substitute for CI.
set -euo pipefail

title="$1"
body="$2"

major_segment() {
  grep -oP '^v?\K\d+' <<<"$1" || true
}

badges="$(grep -oP 'previous-version=[^&]+&new-version=[^&\s")]+' <<<"$body" || true)"
if [ -n "$badges" ]; then
  result="minor-or-patch"
  while IFS= read -r badge; do
    prev="$(grep -oP 'previous-version=\K[^&]+' <<<"$badge")"
    new="$(grep -oP 'new-version=\K[^&\s")]+' <<<"$badge")"
    prev_major="$(major_segment "$prev")"
    new_major="$(major_segment "$new")"
    if [ -z "$prev_major" ] || [ -z "$new_major" ] || [ "$prev_major" != "$new_major" ]; then
      result="major"
    fi
  done <<<"$badges"
  echo "$result"
  exit 0
fi

if [[ "$title" =~ bump\ .+\ from\ ([^[:space:]]+)\ to\ ([^[:space:]]+) ]]; then
  prev="${BASH_REMATCH[1]}"
  new="${BASH_REMATCH[2]}"
  prev_major="$(major_segment "$prev")"
  new_major="$(major_segment "$new")"
  if [ -n "$prev_major" ] && [ -n "$new_major" ]; then
    if [ "$prev_major" != "$new_major" ]; then
      echo "major"
    else
      echo "minor-or-patch"
    fi
    exit 0
  fi
fi

echo "unknown"
