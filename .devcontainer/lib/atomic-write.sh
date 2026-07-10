# shellcheck shell=sh
# Sourced by seedHostConfig.sh and syncConfigOut.sh. Both need the same
# "never write the destination directly" mechanism -- a temp file next to
# the destination, then `mv -f` onto it -- so that a concurrent run of
# either script (another project's container starting or syncing at the
# same moment) can never observe or produce a half-written file at that
# path. Previously each script implemented this inline, twice over in
# seedHostConfig.sh's case (once for a real host file, once for generated
# placeholder content).

# Copies $1 onto $2 atomically, preserving $1's mode/timestamps (cp -p).
atomic_copy() {
  src="$1"
  dest="$2"
  mkdir -p "$(dirname "$dest")"
  tmp="$dest.tmp.$$"
  cp -p "$src" "$tmp"
  mv -f "$tmp" "$dest"
}

# Writes stdin onto $1 atomically.
atomic_write() {
  dest="$1"
  mkdir -p "$(dirname "$dest")"
  tmp="$dest.tmp.$$"
  cat > "$tmp"
  mv -f "$tmp" "$dest"
}
