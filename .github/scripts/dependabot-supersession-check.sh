#!/bin/bash
# Determines whether a Dependabot PR's proposed version bump is already
# superseded by what's actually pinned on the current branch -- i.e. the
# PR would be a same-or-downgrade if merged as-is. This replaces asking
# Dependabot itself via an "@dependabot rebase" comment, which turned out
# to be unusable from automation: Dependabot's command handler checks the
# commenter's repository *collaborator* permission, and a GitHub App's bot
# identity is never a collaborator (that's a separate permission system
# from the App's own installation permissions), so the command is always
# rejected with "only users with push access can use that command."
#
# Reads the currently-pinned version directly from the given files in the
# CURRENT WORKING TREE (expected to be a checkout of the base branch) --
# no extra API calls needed, since the caller already has that checkout.
#
# Usage: dependabot-supersession-check.sh "$title" "$body" < changed_files
#   (changed_files: one repo-relative path per line, from `gh pr diff
#   --name-only` or equivalent)
# Echoes exactly one of: superseded | current | unknown
set -euo pipefail

title="$1"
body="$2"

escape_re() {
  printf '%s' "$1" | sed -e 's/[.[\*^$+?(){}|]/\\&/g'
}

# Strips a leading v/V and keeps only the leading dotted-numeric run, e.g.
# "v4.37.3" -> "4.37.3", "3.14.6-slim" -> "3.14.6". Echoes empty if no
# numeric version could be found at all (e.g. a bare 40-char commit SHA
# for an action with no tagged releases) -- that PR can never be verified
# as superseded by this script, and is deliberately left alone rather than
# guessed at.
normalize_version() {
  local v="$1"
  v="${v#v}"
  v="${v#V}"
  if [[ "$v" =~ ^([0-9]+(\.[0-9]+)*) ]]; then
    echo "${BASH_REMATCH[1]}"
  else
    echo ""
  fi
}

version_ge() {
  [ "$1" = "$2" ] && return 0
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$1" ]
}

# Echoes the version currently pinned for $name in $file, trying each of
# this repo's known pin shapes in turn (pip lockfile, Docker FROM tag,
# GitHub Actions SHA-pin trailing version comment). Returns non-zero if
# $name doesn't appear in $file in any recognized shape.
current_pinned_version() {
  local name="$1" file="$2" pattern hit
  [ -f "$file" ] || return 1
  pattern="$(escape_re "$name")"

  # pip lockfile: name==version (package names are matched case-
  # insensitively; PyPI itself treats -, _ and . as equivalent, but this
  # keeps to a literal match plus case-insensitivity, which covers every
  # dependency this repo currently pins).
  hit="$(grep -iP "^${pattern}==" "$file" 2>/dev/null | grep -oP '==\K[^[:space:];]+' | head -1 || true)"
  if [ -n "$hit" ]; then
    echo "$hit"
    return 0
  fi

  # Docker: FROM name:tag (also matches a registry-qualified name).
  hit="$(grep -oP "(?<![-\w])${pattern}:\K[^[:space:]\"']+" "$file" 2>/dev/null | head -1 || true)"
  if [ -n "$hit" ]; then
    echo "$hit"
    return 0
  fi

  # GitHub Actions: uses: "name@<sha>" # vX.Y.Z
  hit="$(grep -P "${pattern}@[0-9a-f]{40}" "$file" 2>/dev/null | grep -oP '#\s*\K\S+' | head -1 || true)"
  if [ -n "$hit" ]; then
    echo "$hit"
    return 0
  fi

  return 1
}

# Collect every "Updates `name` from X to Y" pair Dependabot generated in
# the body (a grouped PR has one per dependency); fall back to the PR
# title's own "bump name from X to Y" for a single-dependency PR whose
# body lacks that structured section.
# shellcheck disable=SC2016 # literal backticks/\S -- no expansion intended
pairs="$(grep -oP '^Updates `[^`]+` from \S+ to \S+' <<<"$body" || true)"
if [ -z "$pairs" ] && [[ "$title" =~ bump\ ([^[:space:]]+)\ from\ ([^[:space:]]+)\ to\ ([^[:space:]]+) ]]; then
  pairs="Updates \`${BASH_REMATCH[1]}\` from ${BASH_REMATCH[2]} to ${BASH_REMATCH[3]}"
fi

if [ -z "$pairs" ]; then
  echo "unknown"
  exit 0
fi

mapfile -t files

# "unknown" wins over everything else the moment any single pair can't be
# fully resolved -- an unparseable title/version, or a package that never
# turns up in any of the given files (wrong file list, name mismatch, or a
# SHA-only action with no tagged release). Only once every pair resolves
# cleanly does whether they're all superseded actually decide the answer.
any_unresolved=0
all_superseded=1

while IFS= read -r line; do
  [ -z "$line" ] && continue
  if [[ ! "$line" =~ ^Updates\ \`([^\`]+)\`\ from\ ([^[:space:]]+)\ to\ ([^[:space:]]+)$ ]]; then
    any_unresolved=1
    continue
  fi
  name="${BASH_REMATCH[1]}"
  to_raw="${BASH_REMATCH[3]}"
  to_norm="$(normalize_version "$to_raw")"

  if [ -z "$to_norm" ]; then
    any_unresolved=1
    continue
  fi

  # OR across files, not AND: a package pinned in more than one lockfile
  # (e.g. requirements_dev.lock.txt and requirements_test_min.lock.txt) can
  # legitimately sit at different versions per leg, each moving
  # independently (see dependabot.yml's homeassistant/phacc `ignore:`
  # comment for why). Requiring every leg to already meet-or-exceed the
  # proposal missed the real, more common failure: one leg has already
  # moved past the proposed version via its own exact transitive pin (e.g.
  # homeassistant's own orjson requirement), which makes the PR's single
  # version number impossible to apply there -- while another leg, not
  # yet at that exact pin, still looks like a valid forward upgrade and
  # so never flips the old AND-based check to "superseded". Any one leg
  # already at or past the proposed version is sufficient to call the
  # whole pair superseded; the PR was never going to apply cleanly across
  # every leg at once regardless of what the other legs show.
  found_in_any_file=0
  pair_superseded=0
  for file in "${files[@]}"; do
    current_raw="$(current_pinned_version "$name" "$file" || true)"
    [ -z "$current_raw" ] && continue
    found_in_any_file=1
    current_norm="$(normalize_version "$current_raw")"
    if [ -n "$current_norm" ] && version_ge "$current_norm" "$to_norm"; then
      pair_superseded=1
    fi
  done

  if [ "$found_in_any_file" != "1" ]; then
    any_unresolved=1
  elif [ "$pair_superseded" != "1" ]; then
    all_superseded=0
  fi
done <<<"$pairs"

if [ "$any_unresolved" = "1" ]; then
  echo "unknown"
elif [ "$all_superseded" = "1" ]; then
  echo "superseded"
else
  echo "current"
fi
