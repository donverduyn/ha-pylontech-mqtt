# shellcheck shell=sh
# Sourced by postCreate.sh (via lib/sync-config-in.sh, which expects this
# already sourced first -- see postCreate.sh) and by syncConfigOut.sh.
# Previously defined identically in both; kept here once since neither copy
# ever needed to differ.

# Whether $1 is itself the live bind-mount target (see devcontainer.json's
# "mounts"), as opposed to something that still needs copying/staging
# through .agent-sync.
is_bind_mounted() {
  mountpoint -q "$1" 2>/dev/null
}
