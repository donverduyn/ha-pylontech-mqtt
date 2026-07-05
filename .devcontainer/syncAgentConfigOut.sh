#!/bin/sh
# Long-running background helper, started once per container start via
# devcontainer.json's postStartCommand ("... &" — the & is what lets
# postStartCommand return immediately instead of blocking startup on an
# infinite loop). Pushes every write an AI CLI makes to its container-local
# config out to /home/vscode/.agent-sync, the directory bind-mounted from
# ~/.devcontainer-agent-sync/<project> on the host. seedHostAgentConfig.sh
# folds whatever lands there back onto the real host files the next time
# this project's container starts — see that script for why "next start"
# instead of "on stop".
#
# Polling instead of inotifywait: these files change on the order of logins
# and trust prompts (minutes-to-hours apart), not a latency-sensitive path,
# and polling needs no extra package in the base image.
#
# Writes below go through a temp-file-then-rename, same reasoning as
# seedHostAgentConfig.sh: if the same project is open in two containers at
# once, both mount and poll into this one host-side sync directory, and a
# rename can't leave a torn file the way writing the destination directly
# could.
set -e

home="${HOME:-$USERPROFILE}"
sync_dir="$home/.agent-sync"
list_file="$(dirname "$0")/agent-config-files.txt"
pidfile="/tmp/sync-agent-config-out.pid"

# postStartCommand can fire again on a container restart; don't stack a
# second poller on top of one still running from before.
if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
  exit 0
fi
echo $$ > "$pidfile"

while true; do
  while IFS='|' read -r relpath _kind; do
    case "$relpath" in
      ''|'#'*) continue ;;
    esac

    host_path="$home/$relpath"
    sync_path="$sync_dir/$relpath"

    [ -f "$host_path" ] || continue
    if [ ! -f "$sync_path" ] || ! cmp -s "$host_path" "$sync_path"; then
      mkdir -p "$(dirname "$sync_path")"
      tmp="$sync_path.tmp.$$"
      cp -p "$host_path" "$tmp"
      mv -f "$tmp" "$sync_path"
    fi
  done < "$list_file"
  sleep 5
done
