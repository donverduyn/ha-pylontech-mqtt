#!/bin/bash
# Distinguishes Dependabot group PRs from single-dependency PRs. Group PRs
# remain identifiable even when only one update is left in the group, so
# counting generated "Updates ..." sections is not sufficient.
#
# Usage: dependabot-pr-kind.sh "$body"
# Echoes exactly one of: group | single
set -euo pipefail

body="$1"

# Dependabot starts group PR bodies with, for example:
#   Bumps the docker group in /docker with 1 update: python.
#   Bumps the github-actions group with 2 updates:
#   Bumps the github-actions group with 2 updates in the / directory: ...
# The "in <directory>" clause can land either before or after "with N
# updates" depending on Dependabot's own formatting for that PR. If
# Dependabot changes the generated format further, fail closed as a single
# PR so an unknown PR can never enter the automatic merge queue accidentally.
if grep -qE '^Bumps the .+ group( in .+)? with [0-9]+ updates?( in .+)?:' <<<"$body"; then
  echo "group"
else
  echo "single"
fi
