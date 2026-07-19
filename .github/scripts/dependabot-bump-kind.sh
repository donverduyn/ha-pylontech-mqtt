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
# Three independent signals, checked in order:
#
#  1. Dependabot's generated "Updates `NAME` from OLD to NEW" sections in
#     the PR body. A grouped PR contains one of these lines per dependency,
#     including for github-actions and docker where compatibility badges are
#     absent, so this is the most complete signal for a group.
#
#  2. Per-dependency compatibility-score badges in the PR body
#     (a Markdown image link containing `previous-version=X&new-version=Y`
#     per dependency). This remains a fallback for ecosystems/PR formats
#     that provide badges but not generated update sections.
#
#  3. The PR title's own "bump NAME from OLD to NEW" text -- Dependabot's
#     standard title template for a single-dependency PR. This is the
#     fallback for a single-dependency PR whose body has been edited or
#     stripped. A grouped PR's title ("bump the X group with N updates")
#     has no versions, so grouped classification deliberately relies on
#     the per-dependency body lines above.
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

updates="$(grep -oP "^Updates \`[^\`]+\` from \\K[^[:space:]]+ to [^[:space:]]+" <<<"$body" || true)"
if [ -n "$updates" ]; then
  result="minor-or-patch"
  while IFS= read -r update; do
    if [[ "$update" =~ ^([^[:space:]]+)\ to\ ([^[:space:]]+)$ ]]; then
      prev="${BASH_REMATCH[1]}"
      new="${BASH_REMATCH[2]}"
      prev_major="$(major_segment "$prev")"
      new_major="$(major_segment "$new")"
      if [ -z "$prev_major" ] || [ -z "$new_major" ] || [ "$prev_major" != "$new_major" ]; then
        result="major"
      fi
    else
      result="major"
    fi
  done <<<"$updates"
  echo "$result"
  exit 0
fi

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
