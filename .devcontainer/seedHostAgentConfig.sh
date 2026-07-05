#!/bin/sh
# Runs on the HOST (via devcontainer.json's initializeCommand), before the
# container exists — not inside it.
#
# These files used to be bind-mounted straight into the container so every
# AI CLI's login/config would survive a rebuild. That broke: a bind-mounted
# *single file* is bound to an inode, not a path, and every one of these CLIs
# saves via write-temp-file-then-rename. A rename from inside the container
# replaces the inode at that mount point instead of updating it in place,
# which either detaches the mount or (worse, seen in practice) leaves a
# torn/truncated file on the host if the container is closed mid-write —
# and with the host's own Claude Code and a devcontainer's both able to hold
# the same file open at once, that's a race, not a corner case.
#
# Replacement: nothing here is a live mount. Each container gets its own
# plain copy (see postCreate.sh) and stages every change it makes into a
# directory synced out by syncAgentConfigOut.sh — a *directory* bind mount,
# which doesn't have the single-file rename problem since nothing ever
# renames over the mount point itself, only creates fresh files inside it.
# This script is where that staged state gets folded back onto the host
# files, once, before the next container starts (there's no devcontainer
# lifecycle hook that runs on the host when a container stops, so "next
# start" is the earliest safe point to do this).
#
# $sync_dir is this project's own persisted backup, not a mirror of
# whatever's currently authoritative on the host: it's seeded once (the
# first time this project's container ever starts) and after that is only
# ever updated by that project's own container session
# (syncAgentConfigOut.sh) — never overwritten here with the host's current
# state. That matters because the fold-forward below is a whole-file
# replace, not a field-level merge (these JSON blobs mix unrelated concerns —
# OAuth token, project list, MCP registry, trust decisions, startup
# counters — with no way to reconcile field-by-field), so it's inherently
# last-write-wins: if the host, or another project's container, changed the
# shared file since this project's own backup was last updated, that change
# is silently gone from the shared file the moment this fold-forward runs.
# Keeping $sync_dir untouched by anyone else's writes is what bounds that
# loss to "not merged into the live file" rather than "gone" — the discarded
# state still exists, intact, in whichever project's or host's own history
# last had it, and a rebuild always reproduces from *this* project's own
# backup rather than from whatever the shared file happened to look like at
# that moment.
#
# basename of $PWD, not a devcontainer variable: initializeCommand runs with
# cwd set to the local workspace folder, same value
# `${localWorkspaceFolderBasename}` resolves to in devcontainer.json's mounts
# entry for this same sync directory — kept in sync manually, not shared,
# since one runs in a JSON string and the other in a shell script. Two
# checkouts of this repo under the same basename will share (and race for)
# one sync directory; not handled here.
#
# Every path in agent-config-files.txt is global (one copy under $HOME, not
# per-project), so two *different* projects' containers restarting at the
# same moment run this script concurrently against the same host files —
# unlike the container side, this isn't a corner case worth ignoring, since
# it's the same host machine running everything. Two defenses below:
# every write is temp-file-then-rename (rename is atomic on the same
# filesystem, so a concurrent writer can never leave a torn/half-written
# file, only a clean last-write-wins), and the whole run is wrapped in a
# mkdir-based mutex (mkdir is atomic on any POSIX filesystem — used instead
# of flock(1), which macOS doesn't ship) so two concurrent runs serialize
# instead of interleaving their flush/reseed/restage steps at all.
set -e

home="${HOME:-$USERPROFILE}"
list_file="$(dirname "$0")/agent-config-files.txt"
project="$(basename "$PWD")"
sync_dir="$home/.devcontainer-agent-sync/$project"
lock_dir="$home/.devcontainer-agent-sync/.lock"

mkdir -p "$sync_dir"

# Rename onto $2 instead of writing $2 directly, so a concurrent run of this
# same script (another project restarting at the same moment) can never
# observe or produce a half-written file.
atomic_cp() {
  tmp="$2.tmp.$$"
  mkdir -p "$(dirname "$2")"
  cp -p "$1" "$tmp"
  mv -f "$tmp" "$2"
}

seed_json() {
  [ -f "$1" ] || { mkdir -p "$(dirname "$1")" && tmp="$1.tmp.$$" && printf '{}' > "$tmp" && mv -f "$tmp" "$1"; }
}

seed_empty() {
  [ -f "$1" ] || { mkdir -p "$(dirname "$1")" && tmp="$1.tmp.$$" && : > "$tmp" && mv -f "$tmp" "$1"; }
}

# Wait for any other project's concurrent run to finish rather than race it.
# A run here only ever does a handful of small file copies, so a genuinely
# held lock clears in well under a second; ~15s of retries is purely to
# recover from a lock left behind by a run that got SIGKILLed (the EXIT trap
# below can't fire for that), not a realistic wait time.
tries=0
while ! mkdir "$lock_dir" 2>/dev/null; do
  tries=$((tries + 1))
  if [ "$tries" -ge 15 ]; then
    rm -rf "$lock_dir"
    continue
  fi
  sleep 1
done
trap 'rmdir "$lock_dir" 2>/dev/null' EXIT

while IFS='|' read -r relpath kind; do
  case "$relpath" in
    ''|'#'*) continue ;;
  esac

  host_path="$home/$relpath"
  sync_path="$sync_dir/$relpath"

  # Fold this project's own backup onto the shared host file — last-write-wins,
  # see the header comment for exactly what this can and can't lose.
  [ -f "$sync_path" ] && atomic_cp "$sync_path" "$host_path"

  case "$kind" in
    json) seed_json "$host_path" ;;
    empty) seed_empty "$host_path" ;;
  esac

  # Seed this project's own backup once, only if it has never existed —
  # after this first time, it's only ever updated by this project's own
  # container session, never refreshed from the shared file here.
  [ -f "$sync_path" ] || atomic_cp "$host_path" "$sync_path"
done < "$list_file"

# GitHub Copilot CLI: whole directory, not staged file-by-file (see
# agent-config-files.txt) — still a plain bind mount since its login lives
# inside session-store.db (SQLite) alongside its own cache/session state, so
# there's no single "the auth file" to isolate and rename-over-mount doesn't
# apply the same way to a directory mount.
mkdir -p "$home/.copilot"
