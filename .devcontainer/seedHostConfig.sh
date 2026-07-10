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
# Replacement: config-files.txt lists every one of these paths, most
# of them whole directories. A *directory* bind mount doesn't
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
# inside its own directory, so it can't be bind-mounted the way the
# directory paths are — same single-file rename problem as the very first
# paragraph above. It still gets the old file-by-file treatment (seeded
# here once; see postCreate.sh and syncConfigOut.sh for the copy-in/poll-out
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
list_file="$(dirname "$0")/config-files.txt"
sync_dir="$home/.devcontainer-agent-sync$PWD"

mkdir -p "$sync_dir"

# VS Code Local History is a devcontainer bind mount too, but it is not an
# agent config path and does not belong in config-files.txt. Create the
# host-side source before Docker evaluates the mount.
mkdir -p "$sync_dir/.vscode-server/data/User/History"

# atomic_copy/atomic_write (write onto a temp file next to the destination,
# then `mv -f` onto it, so a concurrent run of this same script -- another
# project restarting at the same moment -- can never observe or produce a
# half-written file) are shared with syncConfigOut.sh -- see lib/fs.sh.
# shellcheck disable=SC1091 # path is repo-local and always present
. "$(dirname "$0")/lib/fs.sh"

# Whether $1 (a relpath from config-files.txt) is declared as a directory
# bind mount under $HOME in devcontainer.json's "mounts" array, for the
# sole case below where the host doesn't have anything at that path yet to
# just look at. Everything about how that's determined -- reading the
# file, its JSONC-ness, the trailing-slash convention -- lives entirely in
# here; nothing outside this function needs to know any of it.
#
# Reusing devcontainer.json's own declarations isn't a new hand-maintained
# list -- they already have to be exactly right for the real bind mount to
# attach at all. devcontainer.json is JSONC (comments, trailing commas),
# and nothing can parse that on the host side: this script runs via
# initializeCommand, before the devcontainer -- and anything it could
# install, like jq or @devcontainers/cli -- exists, and installing
# something onto the host itself (as opposed to the disposable container)
# is a different, more invasive class of action this project doesn't do
# anywhere else. lib/json.sh (see that file) converts it to
# strict JSON first, in pure bash -- not awk/jq/python, since bash is
# already the one thing this script unconditionally requires on the host
# regardless (see devcontainer.json's initializeCommand), so this adds no
# new tool the host has to have. Does it properly, respecting string
# literals, not by a naive "strip lines starting with //" that a mount
# path or comment containing "//" or a trailing comma inside a quoted
# value could fool.
#
# The trailing "/" required right before ",type=bind" is devcontainer.json's
# own explicit directory-mount marker (see that file's "mounts" comment) --
# not an inference from "this relpath appears in the array at all", which
# would wrongly assume every type=bind target under /home/vscode/ is a
# directory with no way to tell a hypothetical file-target bind mount
# apart. Docker itself normalizes the trailing slash away transparently
# (confirmed empirically against a real daemon), so requiring it here
# changes nothing about how the mount actually behaves. What's left below
# is then matched with `case` against a whole space-delimited token, not a
# substring grep: that's what actually keeps e.g. ".claude" from ever being
# confused with a differently-typed path like a hypothetical ".claude.json"
# mount line, not a coincidence of what today's mounts array happens to
# contain.
#
# The set is parsed at most once per script run -- computed lazily on
# first call and cached in _dir_mount_relpaths, since every call after the
# first just needs the same unchanging set devcontainer.json already had
# at the start of this run.
#
# Each mount spec's comma-separated fields are read by key (target=.../,
# type=bind), not matched as one glued "target=...,/type=bind" substring --
# that used to require type=bind to sit immediately after target= with
# nothing else in between, so a hypothetical third field wedged between
# them (e.g. consistency=cached) would silently fall through to "not a
# declared dir mount" even though the mount really is one. Docker mount
# strings don't guarantee field order, so this must not either. This
# mirrors tests/test_devcontainer_json.py's own
# _declared_dir_mount_relpaths() Python helper, which already parsed it
# this way.
is_declared_dir_mount() {
  # Saved before the loop below: it does `set -- $mount_str` to split each
  # mount spec's fields, which overwrites this function's own positional
  # params -- $1 itself would otherwise be clobbered by the last mount spec
  # processed, breaking the case check at the bottom on this (first,
  # cache-populating) call.
  relpath_to_check="$1"
  if [ -z "${_dir_mount_relpaths+x}" ]; then
    _dir_mount_relpaths=" "
    while IFS= read -r mount_str; do
      [ -n "$mount_str" ] || continue
      is_bind=0
      target_relpath=""
      old_ifs=$IFS
      IFS=,
      # shellcheck disable=SC2086 # intentional word-splitting: this is how the comma-separated fields get split into positional params
      set -- $mount_str
      IFS=$old_ifs
      for field in "$@"; do
        case "$field" in
          type=bind) is_bind=1 ;;
          target=/home/vscode/*/) target_relpath=${field#target=/home/vscode/} ;;
        esac
      done
      if [ "$is_bind" -eq 1 ] && [ -n "$target_relpath" ]; then
        _dir_mount_relpaths="$_dir_mount_relpaths${target_relpath%/} "
      fi
    done <<EOF
$(bash "$(dirname "$0")/lib/json.sh" "$(dirname "$0")/devcontainer.json" |
      grep -oE '"[^"]*target=/home/vscode/[^"]*"' |
      sed -E 's/^"//; s/"$//')
EOF
  fi
  case "$_dir_mount_relpaths" in
    *" $relpath_to_check "*) return 0 ;;
    *) return 1 ;;
  esac
}

# The one signal this project defines for what an invented placeholder
# should look like when nothing real exists anywhere for a bare-file entry
# (see below) -- keyed on the relpath's own extension rather than a
# hand-maintained label, since config-files.txt carries no per-entry
# metadata anymore (see this file's header). Only .claude.json exists
# today, hence the one real case; a future non-JSON bare-file entry would
# need a case added here.
default_content_for_relpath() {
  case "$1" in
    *.json) printf '{}' ;;
    *) printf '' ;;
  esac
}

while IFS= read -r relpath; do
  case "$relpath" in
    ''|'#'*) continue ;;
  esac

  host_path="$home/$relpath"
  sync_path="$sync_dir/$relpath"

  # Already seeded for this project on a prior run — leave it alone from
  # here on, whichever kind it is.
  if [ -e "$sync_path" ]; then
    continue
  fi

  # "dir or file" is decided against whatever's actually on the host at
  # this path -- checked once each, in priority order, not re-tested
  # inside a branch after already being implied by getting there.
  if [ -d "$host_path" ]; then
    # A real, existing directory on the host -- copy its content in.
    mkdir -p "$sync_path"
    cp -a "$host_path/." "$sync_path/"
    # Dropped inside $sync_path itself (not next to it) so it rides the
    # same bind mount into the container: postCreate.sh's sync_config_in
    # uses this to run its one-time full ownership walk only on the first
    # rebuild after a fresh seed like this one, instead of on every rebuild
    # forever, for the two config dirs (.claude, .codex) large enough for
    # that walk to actually cost something.
    touch "$sync_path/.devcontainer-freshly-seeded"
  elif [ -f "$host_path" ]; then
    # A real, existing bare file (.claude.json, almost always) -- only ever
    # read from the host here, never written to it.
    atomic_copy "$host_path" "$sync_path"
  elif is_declared_dir_mount "$relpath"; then
    # Nothing on the host yet -- a tool that's simply never been used on
    # this host (gh/kilo/opencode are all plausible first-run-on-this-
    # machine cases, not just theoretical) -- but devcontainer.json still
    # declares a directory mount for it, so an empty staged directory is
    # what's needed for that mount to attach correctly once the container
    # is created.
    mkdir -p "$sync_path"
    touch "$sync_path/.devcontainer-freshly-seeded"
  else
    # Nothing on the host, and not a declared directory mount either -- a
    # file-type entry with no real content anywhere yet. Still seeded, but
    # only in $sync_path, never on the real host: postCreate.sh's job (see
    # lib/fs.sh) is purely to copy whatever's already staged
    # into the container, never to invent content itself, so something has
    # to already exist here for it to copy from. The tradeoff, same as
    # every other path here: seeded once, ever (see the "already seeded"
    # check above) -- if the host later gets real content at this path
    # (e.g. the user logs into Claude Code natively on this machine
    # sometime after this project's first-ever container build), this
    # project's staged copy won't pick that up on its own; deleting
    # $sync_path forces a fresh re-seed.
    default_content_for_relpath "$relpath" | atomic_write "$sync_path"
  fi
done < "$list_file"
