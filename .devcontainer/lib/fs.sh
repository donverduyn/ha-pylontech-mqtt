# shellcheck shell=sh
# Filesystem-domain helpers shared across the devcontainer setup scripts:
#   is_bind_mounted    -- sync_config_in below, and syncConfigOut.sh
#   atomic_copy/atomic_write -- seedHostConfig.sh and syncConfigOut.sh
#   sync_config_in     -- postCreate.sh only
# Kept as its own file (not inlined in any of those) so tests can source
# just this -- postCreate.sh's and seedHostConfig.sh's own top-level bodies
# run real installs/downloads/sudo calls unconditionally the moment they're
# invoked, which a unit test has no business triggering.

# Whether $1 is itself the live bind-mount target (see devcontainer.json's
# "mounts"), as opposed to something that still needs copying/staging
# through .agent-sync.
is_bind_mounted() {
  mountpoint -q "$1" 2>/dev/null
}

# Copies $1 onto $2 atomically, preserving $1's mode/timestamps (cp -p): a
# temp file next to the destination, then `mv -f` onto it, so a concurrent
# run of another script (another project's container starting or syncing
# at the same moment) can never observe or produce a half-written file at
# that path.
atomic_copy() {
  src="$1"
  dest="$2"
  mkdir -p "$(dirname "$dest")"
  tmp="$dest.tmp.$$"
  cp -p "$src" "$tmp"
  mv -f "$tmp" "$dest"
}

# Writes stdin onto $1 atomically -- same mechanism as atomic_copy above.
atomic_write() {
  dest="$1"
  mkdir -p "$(dirname "$dest")"
  tmp="$dest.tmp.$$"
  cat > "$tmp"
  mv -f "$tmp" "$dest"
}

full_ownership_walk() {
  walk_relpath=$1
  # -prune on .git: git marks pack files read-only (mode 444), and on
  # Docker Desktop for Mac, bind-mount chown is proxied back to the host
  # filesystem, where changing ownership of a read-only file reliably
  # fails with "Permission denied" even under sudo in the container —
  # confirmed in practice for claude-plugins-official's .git/objects/pack
  # (a marketplace plugin cloned by Claude Code itself). Skipping .git
  # entirely avoids the failure outright instead of swallowing it after
  # the fact: nothing needs to change ownership of a git repo's internals
  # anyway, since mode 444 is already world-readable (vscode can read
  # these files regardless of who owns them) and nothing here writes to
  # an already-packed git object. `|| true` still guards the remaining
  # walk in case some other unrelated file trips the same fakeowner
  # quirk.
  #
  # chown -h: without -h, chown dereferences symlinks and chowns their
  # *target* instead of the link itself. Codex leaves dangling sandbox
  # symlinks behind under .codex/tmp/** (applypatch, apply_patch,
  # codex-execve-wrapper) whose targets it has already cleaned up by the
  # time this runs, so a target-following chown fails with "cannot
  # dereference ... No such file or directory" for each one (confirmed in
  # practice). -h fixes the symlink's own ownership instead, which always
  # exists, sidestepping the dangling-target case entirely.
  sudo find "$HOME/$walk_relpath" \( -name .git -prune \) -o -exec chown -h vscode:vscode {} + || true
}

sync_config_in() {
  relpath=$1
  target="$HOME/$relpath"
  staged="$HOME/.agent-sync/$relpath"

  if is_bind_mounted "$target"; then
    # Docker mounted this path directly onto its host-backed source (see
    # devcontainer.json's "mounts") -- already live, nothing to copy. The
    # one thing still needed: seedHostConfig.sh drops a marker inside a
    # freshly-seeded path the one time it populates it fresh from the host,
    # since that's the only moment content with a foreign (host) UID can
    # exist here on this fakeowner-typed mount -- a file nobody has ever
    # explicitly chowned shows each caller as its own owner by default, and
    # an explicit chown persists as real, caller-independent ownership from
    # then on.
    fresh_marker="$target/.devcontainer-freshly-seeded"
    if [ -d "$target" ] && [ -e "$fresh_marker" ]; then
      full_ownership_walk "$relpath"
      sudo rm -f "$fresh_marker"
    fi
    return 0
  fi

  # Not mounted -- either this path has no "mounts" entry by design (a bare
  # file like .claude.json can't be bind-mounted the same way a directory
  # can, see devcontainer.json's "mounts" comment) or a mount that should
  # exist didn't attach. Either way, fall back to copying from the staging
  # mirror: .agent-sync is bind-mounted from this project's *entire*
  # host-side backup (see devcontainer.json's "mounts"), not just
  # .claude.json, so .agent-sync/$relpath exists for every entry in
  # config-files.txt -- seedHostConfig.sh (host-side, before this ever
  # runs) is what decides real-vs-default content and stages it into
  # .agent-sync; this function's only job is copying whatever's already
  # there into the container, never inventing content of its own. This
  # means a missing/failed mount degrades to a copy instead of silently
  # losing data. Always overwrites rather than merging: the image itself
  # can ship a default at this path (the claude-code feature's install
  # step does exactly this for .claude.json -- confirmed via `docker run
  # --rm <image> stat /home/vscode/.claude.json` showing it present, with
  # a build-time mtime, before any postCreate.sh code ever runs), so
  # trusting whatever's already there over the staged copy would silently
  # resurrect that placeholder instead of the real synced state.
  [ -e "$staged" ] || return 0
  mkdir -p "$(dirname "$target")" || return $?
  if [ -d "$staged" ]; then
    rsync -a --delete "$staged/" "$target/"
  else
    cp -p "$staged" "$target"
  fi
}
