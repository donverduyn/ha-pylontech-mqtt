#!/bin/sh
# Long-running background helper. Launched once from postCreate.sh (after
# its copy-in loop -- see postCreate.sh for why that order matters) and
# again via devcontainer.json's postStartCommand/postAttachCommand as a
# fallback in case that first launch got missed or killed by session
# teardown; the pidfile guard below makes every launch after the first a
# no-op.
#
# Every path in config-files.txt that's actually bind-mounted (see
# devcontainer.json's "mounts") needs no help here -- the container's writes
# already land on the host's per-project backup directly. What's left,
# checked live via is_bind_mounted() rather than a label (see
# config-files.txt's header for why), is whatever ISN'T mounted: today
# that's just .claude.json (a bare file that can't be bind-mounted the same
# way a directory can -- see seedHostConfig.sh), but the same mechanism
# covers any directory whose mount didn't attach for whatever reason too,
# instead of that silently never syncing.
#
# inotifywait watches each unmounted path recursively (harmless no-op for a
# plain file) for close_write/moved_to/create/moved_from/delete, then
# re-syncs whichever tracked path the event fell under. Watching the
# *directory*, not a target file's own inode, survives the
# write-temp-then-rename pattern these CLIs use to save: a watch on the
# target file's inode goes stale the instant a rename replaces it, same
# reasoning as why a directory bind mount survives renames but a
# single-file one doesn't (see seedHostConfig.sh).
#
# No debounce: sync_path() below is a cheap idempotent no-op when the paths
# already match, so a burst of events (e.g. the temp file's own close_write
# followed by the rename's moved_to) just costs a couple of harmless extra
# comparisons, not a correctness issue.
#
# Writes below go through a temp-file-then-rename for plain files, same
# reasoning as seedHostConfig.sh: if the same project is open in two
# containers at once, both watch and write into this one host-side sync
# directory, and a rename can't leave a torn file the way writing the
# destination directly could. Directories go through rsync --delete
# instead, which is atomic per-file already and needs to handle adds and
# deletes within the tree, not just whole-file replacement.
set -e

home="${HOME:-$USERPROFILE}"
sync_dir="$home/.agent-sync"
list_file="$(dirname "$0")/config-files.txt"
pidfile="/tmp/sync-agent-config-out.pid"

# postStartCommand/postAttachCommand can fire again after postCreate.sh
# already launched this once, or on a container restart; don't stack a
# second watcher on top of one still running from before.
if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
  exit 0
fi
echo $$ > "$pidfile"

# is_bind_mounted/atomic_copy live in utils/fs.sh, shared with postCreate.sh
# and seedHostConfig.sh respectively -- see that file.
# shellcheck disable=SC1091 # path is repo-local and always present
. "$(dirname "$0")/utils/fs.sh"

sync_path() {
  relpath="$1"
  live="$home/$relpath"
  staged="$sync_dir/$relpath"

  [ -e "$live" ] || return 0

  if [ -d "$live" ]; then
    rsync -a --delete "$live/" "$staged/"
  else
    if [ ! -f "$staged" ] || ! cmp -s "$live" "$staged"; then
      atomic_copy "$live" "$staged"
    fi
  fi
}

# Paths from config-files.txt that aren't actually bind-mounted right now --
# computed once at startup, since mount status doesn't change over a
# container's lifetime. Catch up on anything that changed while no watcher
# was running (e.g. between this project's last container and this one) at
# the same time.
unmounted_paths=""
watch_targets=""
while IFS= read -r relpath; do
  case "$relpath" in
    ''|'#'*) continue ;;
  esac
  is_bind_mounted "$home/$relpath" && continue
  unmounted_paths="$unmounted_paths $relpath"
  watch_targets="$watch_targets $home/$relpath"
  sync_path "$relpath"
done < "$list_file"

# Nothing to watch (every path is live-mounted) -- exit instead of running
# inotifywait with no targets, which would just error.
[ -n "$unmounted_paths" ] || exit 0

# shellcheck disable=SC2086 # intentional word-splitting: each entry is one watch target, and none contain spaces
inotifywait -m -q -r -e close_write -e moved_to -e create -e moved_from -e delete --format '%w%f' $watch_targets |
  while IFS= read -r changed; do
    for relpath in $unmounted_paths; do
      case "$changed" in
        "$home/$relpath" | "$home/$relpath"/*) sync_path "$relpath" ;;
      esac
    done
  done
