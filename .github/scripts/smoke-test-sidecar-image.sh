#!/bin/bash
# Asserts that the built sidecar image fails fast, and for the right reason,
# when its required configuration is absent: exit code 1 from the config
# validation path, with the MQTT_BROKER message on the way out.
#
# Invoked by .github/actions/build-sidecar-image against both newly built and
# cache-restored bytes. The cache key proves a restored archive is
# byte-identical, but the runner's Docker/runtime environment can change
# independently and still needs exercising.
#
# Lives here rather than inline in that composite action's `run:` block so
# meta-lint's ShellCheck step covers it -- actionlint does not lint composite
# actions. See .github/scripts/create-or-update-pr.sh's header.
set -euo pipefail

image="${1:-pylon2mqtt:ci}"

set +e
output="$(docker run --rm "$image" 2>&1)"
code=$?
set -e

echo "$output"

if [ "$code" -ne 1 ]; then
  echo "::error::Expected exit code 1 (fail-fast config validation), got $code"
  exit 1
fi

if ! grep -q "MQTT_BROKER environment variable is required" <<<"$output"; then
  echo "::error::Expected fail-fast MQTT_BROKER validation message was not logged"
  exit 1
fi
