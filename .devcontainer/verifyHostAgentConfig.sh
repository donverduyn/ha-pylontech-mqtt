#!/bin/sh
# Run on the HOST (not inside the devcontainer) to confirm every path in
# agent-config-files.txt is actually present and populated under $HOME
# before rebuilding.
#
# This only reflects what a *fresh* per-project seed would pull in — for a
# project whose backup already exists under
# ~/.devcontainer-agent-sync/<absolute-path>, seedHostAgentConfig.sh never
# looks at these host paths again (see that script), so an existing
# project's container may already have newer or different content than
# what's checked here. This script is most useful before a project's very
# first build, or after intentionally deleting its backup to force a
# re-seed — a missing/empty path here means that CLI will come up logged
# out / unconfigured the next time either of those happens.
set -e

home="${HOME:-$USERPROFILE}"
ok=0
empty=0
missing=0

check() {
  path="$1"
  label="$2"
  if [ ! -e "$path" ]; then
    printf 'MISSING  %-9s %s\n' "$label" "$path"
    missing=$((missing + 1))
  elif [ -d "$path" ]; then
    count=$(find "$path" -type f | wc -l | tr -d ' ')
    if [ "$count" -eq 0 ]; then
      printf 'EMPTY    %-9s %s (no files inside)\n' "$label" "$path"
      empty=$((empty + 1))
    else
      printf 'OK       %-9s %s (%s files)\n' "$label" "$path" "$count"
      ok=$((ok + 1))
    fi
  elif [ ! -s "$path" ]; then
    printf 'EMPTY    %-9s %s (0 bytes)\n' "$label" "$path"
    empty=$((empty + 1))
  elif [ "$(cat "$path")" = "{}" ]; then
    # seedHostAgentConfig.sh's placeholder for JSON files it didn't find
    # already present — never actually seeded with real content.
    printf 'EMPTY    %-9s %s ({} placeholder)\n' "$label" "$path"
    empty=$((empty + 1))
  else
    printf 'OK       %-9s %s (%s bytes)\n' "$label" "$path" "$(wc -c < "$path" | tr -d ' ')"
    ok=$((ok + 1))
  fi
}

check "$home/.claude" "claude"
check "$home/.claude.json" "claude"
check "$home/.codex" "codex"
check "$home/.config/opencode" "opencode"
check "$home/.local/share/opencode" "opencode"
check "$home/.config/kilo" "kilo"
check "$home/.config/gh" "gh"
check "$home/.gemini" "gemini"
check "$home/.copilot" "copilot"

echo
echo "$ok ok, $empty empty/placeholder, $missing missing"
[ "$missing" -eq 0 ] && [ "$empty" -eq 0 ]
