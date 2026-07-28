#!/bin/bash
# Determines whether a Dependabot PR proposes a version for a package that
# the currently-locked homeassistant/pytest-homeassistant-custom-component
# (phacc) releases pin exactly -- i.e. a PR that cannot resolve no matter
# how often it is rebased, because uv will always report e.g.
#
#   Because pytest-homeassistant-custom-component==0.13.308 depends on
#   pytest==9.0.0 and you require pytest==9.0.3, we can conclude that your
#   requirements and pytest-homeassistant-custom-component==0.13.308 are
#   incompatible.
#
# dependabot.yml deliberately keeps these names out of Dependabot's global
# `ignore:` list and only excludes them from the security-updates group, so
# that a security advisory against one of them still surfaces as its own
# visible PR rather than silently blocking the whole group (see
# scripts/update_dependencies.py:update_dependabot_exclude_list). That choice
# is preserved here: the PR is still opened, and still visible. What this
# check removes is the seven days of red CI that followed, during which the
# PR sat failing `pytest (min)` until close-stale-automation-prs.yaml's
# generic age sweep closed it. daily-pr-sync.sh closes it within a day
# instead, naming the specific package and the real fix path.
#
# The list is read from dependabot.yml's generated exclude-patterns block in
# the CURRENT WORKING TREE (expected to be a checkout of the base branch)
# rather than recomputed, so it is exactly the set
# collect_dependabot_exclude_names() derived from both legs' locked
# homeassistant/phacc releases the last time `make update-deps` ran.
#
# Usage: dependabot-phacc-pinned-check.sh "$title" "$body"
# Echoes exactly one of: blocked | ok, and exits 0.
#
# Two different unknowns, deliberately handled differently:
#
#   - An unrecognised PR title/body fails OPEN (ok, exit 0). Closing a PR is
#     not reversible by this automation, so a PR this cannot classify is left
#     to the existing rebase/check-wait path exactly as before.
#   - A dependabot.yml this cannot read the generated block out of fails LOUD
#     (exit 1, ::error::). That is not an unclassifiable PR, it is this
#     repository's own file no longer matching the format
#     update_dependabot_exclude_list() writes -- an indent change, single
#     quotes, a renamed marker, a YAML dumper. Returning "ok" there would
#     silently disable this check forever and bring back the exact symptom it
#     exists to prevent, with nothing anywhere reporting a problem.
set -euo pipefail

title="$1"
body="$2"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
dependabot_yml="${DEPENDABOT_YML:-$script_dir/../dependabot.yml}"

fail_loud() {
  echo "::error::$1" >&2
  exit 1
}

[ -f "$dependabot_yml" ] || fail_loud "$dependabot_yml does not exist; cannot tell which packages homeassistant/phacc pin exactly."

# Everything between the generated markers, one bare package name per line.
# Anchored on the markers rather than on "exclude-patterns:" so a
# hand-written exclusion added outside the generated block is never treated
# as an exact pin.
pinned="$(
  sed -n '/# <dependabot-exclude-generated>/,/# <\/dependabot-exclude-generated>/p' \
    "$dependabot_yml" \
    | grep -oP '^\s*-\s*"\K[^"]+' \
    || true
)"
[ -n "$pinned" ] || fail_loud "No generated exclude-patterns entries found between the dependabot-exclude-generated markers in $dependabot_yml; regenerate it with \`make update-deps\` or fix scripts/update_dependencies.py:update_dependabot_exclude_list."

# Same two signals dependabot-bump-kind.sh and
# dependabot-supersession-check.sh read, in the same order: Dependabot's
# generated per-dependency "Updates `name` from X to Y" body sections
# (present once per dependency on a grouped PR), falling back to the single-
# dependency title template for a PR whose body has been edited or stripped.
# shellcheck disable=SC2016 # literal backticks -- no expansion intended
names="$(grep -oP '^Updates `\K[^`]+' <<<"$body" || true)"
if [ -z "$names" ] && [[ "$title" =~ bump\ ([^[:space:]]+)\ from\ [^[:space:]]+\ to\ [^[:space:]]+ ]]; then
  names="${BASH_REMATCH[1]}"
fi
[ -n "$names" ] || { echo "ok"; exit 0; }

# The bash equivalent of scripts/update_dependencies.py's
# normalize_pypi_name(), which is what wrote the list being read back here:
# `re.sub(r"[-_.]+", "-", name).lower()`. Must stay equivalent to it -- a
# character-wise `tr '._' '--'` is not, because it cannot collapse runs, so
# the two sides would disagree on exactly the names this comparison exists
# to match. Both sides are normalised (rather than trusting the generated
# list to already be) so the shapes Dependabot renders in a PR title --
# "Paho-MQTT", "paho.mqtt" -- compare equal to the "paho-mqtt" written here.
normalize_names() {
  tr '[:upper:]' '[:lower:]' | sed -E 's/[-_.]+/-/g'
}

pinned="$(normalize_names <<<"$pinned")"

# Any one exactly-pinned package is enough: Dependabot proposes a single
# version per dependency across every manifest it appears in, so one
# unsatisfiable member makes the whole PR unresolvable. A grouped PR should
# never contain one (that is what exclude-patterns is for), but checking
# every name rather than assuming keeps this correct if grouping changes.
while IFS= read -r name; do
  [ -z "$name" ] && continue
  if grep -Fxq -- "$name" <<<"$pinned"; then
    echo "blocked"
    exit 0
  fi
done < <(normalize_names <<<"$names")

echo "ok"
