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
# Replacement: agent-config-files.txt lists every one of these paths, most
# of them whole directories (kind "dir"). A *directory* bind mount doesn't
# have the single-file problem above — renaming a file inside a mounted
# directory only replaces that entry, never the mount point itself — so
# this project's own backup directory ($sync_dir below) gets bind-mounted
# straight onto the container's real path for each tool (see
# devcontainer.json's "mounts"). This script's only job for those is to
# seed $sync_dir once, ever, the very first time this project's container
# starts, from whatever the host happens to have at that moment.
#
# A prior version of this script instead folded $sync_dir back onto the
# host's file on every rebuild. That was a whole-file replace, not a
# field-level merge (these JSON blobs mix unrelated concerns — OAuth token,
# project list, MCP registry, trust decisions, startup counters — with no
# way to reconcile field-by-field), so it was inherently last-write-wins:
# confirmed in practice, not just in theory, to silently discard a host-side
# edit made between two of this project's container starts. That
# fold-forward is gone. After the first seed, this script never reads from
# or writes to the host's own copy of a "dir" path again — the live bind
# mount is the only thing anyone reads or writes from that point on, so
# there's nothing left to fold forward, and no way for this project's
# container or another project's to stomp on the host's or each other's
# state.
#
# .claude.json is the one exception: it's a bare file at $HOME root, not
# inside its own directory, so it can't be bind-mounted the way the "dir"
# paths are — same single-file rename problem as the very first paragraph
# above. It still gets the old file-by-file treatment (seeded here once;
# see postCreate.sh and syncAgentConfigOut.sh for the copy-in/poll-out
# around it), just scoped to this one file instead of every path.
#
# $sync_dir is keyed on this project's absolute path, not its basename —
# initializeCommand runs with cwd set to the local workspace folder, the
# same value ${localWorkspaceFolder} resolves to in devcontainer.json's
# mounts entries for this same tree (kept in sync manually, not shared,
# since one runs in a JSON string and the other in a shell script). Basename
# alone would let two checkouts of differently-located repos that happen to
# share a folder name collide on one sync directory; the absolute path
# can't collide that way. (Existing per-project backups seeded under the
# old basename-keyed path are simply orphaned by this change — the next
# build re-seeds fresh from whatever the host has, same as any first-ever
# build.)
set -e

home="${HOME:-$USERPROFILE}"
list_file="$(dirname "$0")/agent-config-files.txt"
sync_dir="$home/.devcontainer-agent-sync$PWD"

mkdir -p "$sync_dir"

# VS Code Local History is a devcontainer bind mount too, but it is not an
# agent config path and does not belong in agent-config-files.txt. Create the
# host-side source before Docker evaluates the mount.
mkdir -p "$sync_dir/.vscode-server/data/User/History"

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

# Two *different* projects' containers starting at the same moment could
# both see .claude.json missing on the host and both create the {}
# placeholder for it. No lock needed: seed_json and atomic_cp both write via
# a per-process-unique temp file + atomic mv, so both concurrent runs land
# the same content without ever producing a torn file.
while IFS='|' read -r relpath kind; do
  case "$relpath" in
    ''|'#'*) continue ;;
  esac

  host_path="$home/$relpath"
  sync_path="$sync_dir/$relpath"

  # Already seeded for this project on a prior run — leave it alone from
  # here on, whichever kind it is.
  [ -e "$sync_path" ] && continue

  case "$kind" in
    dir)
      mkdir -p "$sync_path"
      [ -d "$host_path" ] && cp -a "$host_path/." "$sync_path/"
      ;;
    json)
      seed_json "$host_path"
      atomic_cp "$host_path" "$sync_path"
      ;;
  esac
done < "$list_file"
