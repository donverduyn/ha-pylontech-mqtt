#!/bin/sh
# Long-running background helper, started once per container start via
# devcontainer.json's postStartCommand (nohup'd and backgrounded so that
# command returns immediately instead of blocking startup on the infinite
# loop below — see devcontainer.json for why nohup specifically, not just
# a bare "&").
#
# Every "dir"-kind path in agent-config-files.txt is a live bind mount
# straight onto its real container path — the container's writes already
# land on the host's per-project backup directly, no help needed here. The
# one exception is .claude.json ("json"-kind): a bare file at $HOME root
# that can't be bind-mounted the same way (see seedHostAgentConfig.sh), so
# it still needs pushing out to /home/vscode/.agent-sync (the directory
# bind-mounted from this project's backup on the host) by hand. This script
# only ever seeds that flow, once ever — see seedHostAgentConfig.sh for why
# nothing folds it back onto the host's own ~/.claude.json beyond that.
#
# inotifywait watches $home itself, not the individual json-kind files —
# these CLIs save via write-temp-then-rename, and a watch placed on the
# *target* file's inode goes stale the instant that rename happens (the
# watched inode is the old file being replaced, not the one that lands at
# that name). Watching the containing directory for close_write/moved_to
# and filtering by the reported filename survives renames indefinitely,
# same principle as why a directory bind mount survives them but a
# single-file one doesn't (see seedHostAgentConfig.sh). Every json-kind
# path is, by construction, a bare file directly under $home (that's the
# whole reason it's in this kind instead of "dir"), so a single
# non-recursive watch on $home covers all of them without watching
# anything under the bind-mounted "dir" subdirectories.
#
# No debounce: sync_one() below is a cheap idempotent no-op when the files
# already match, so a burst of events (e.g. the temp file's own close_write
# followed by the rename's moved_to) just costs a couple of harmless extra
# comparisons, not a correctness issue.
#
# Writes below go through a temp-file-then-rename, same reasoning as
# seedHostAgentConfig.sh: if the same project is open in two containers at
# once, both watch and write into this one host-side sync directory, and a
# rename can't leave a torn file the way writing the destination directly
# could.
set -e

home="${HOME:-$USERPROFILE}"
sync_dir="$home/.agent-sync"
list_file="$(dirname "$0")/agent-config-files.txt"
pidfile="/tmp/sync-agent-config-out.pid"

# postStartCommand can fire again on a container restart; don't stack a
# second watcher on top of one still running from before.
if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
  exit 0
fi
echo $$ > "$pidfile"

sync_one() {
  relpath="$1"
  host_path="$home/$relpath"
  sync_path="$sync_dir/$relpath"

  [ -f "$host_path" ] || return 0
  if [ ! -f "$sync_path" ] || ! cmp -s "$host_path" "$sync_path"; then
    mkdir -p "$(dirname "$sync_path")"
    tmp="$sync_path.tmp.$$"
    cp -p "$host_path" "$tmp"
    mv -f "$tmp" "$sync_path"
  fi
}

sync_all_json() {
  while IFS='|' read -r relpath kind; do
    case "$relpath" in
      ''|'#'*) continue ;;
    esac
    [ "$kind" = "json" ] || continue
    sync_one "$relpath"
  done < "$list_file"
}

# Catch up on anything that changed while no watcher was running (e.g.
# between this project's last container and this one).
sync_all_json

inotifywait -m -q -e close_write -e moved_to --format '%f' "$home" |
  while IFS= read -r changed; do
    while IFS='|' read -r relpath kind; do
      case "$relpath" in
        ''|'#'*) continue ;;
      esac
      [ "$kind" = "json" ] || continue
      [ "$relpath" = "$changed" ] && sync_one "$relpath"
    done < "$list_file"
  done
