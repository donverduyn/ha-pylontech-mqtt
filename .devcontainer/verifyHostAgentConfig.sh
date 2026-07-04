#!/bin/sh
# Run on the HOST (not inside the devcontainer) to confirm every file
# .devcontainer/devcontainer.json's `mounts` expects is actually present
# under $HOME before rebuilding — a missing file here means that CLI will
# come up logged out / unconfigured after the rebuild, same as a fresh
# install. Companion to seedHostAgentConfig.sh, which only guarantees a
# file *exists* (possibly as an empty placeholder); this checks whether it
# actually has real content in it.
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

check "$home/.claude/settings.json" "claude"
check "$home/.claude/.credentials.json" "claude"
check "$home/.claude.json" "claude"
check "$home/.codex/auth.json" "codex"
check "$home/.codex/config.toml" "codex"
check "$home/.config/opencode/opencode.jsonc" "opencode"
check "$home/.local/share/opencode/auth.json" "opencode"
check "$home/.config/kilo/kilo.jsonc" "kilo"
check "$home/.config/gh/hosts.yml" "gh"
check "$home/.config/gh/config.yml" "gh"
check "$home/.copilot" "copilot"
check "$home/.gemini/oauth_creds.json" "gemini"
check "$home/.gemini/google_accounts.json" "gemini"
check "$home/.gemini/settings.json" "gemini"
check "$home/.gemini/trustedFolders.json" "gemini"
check "$home/.gemini/antigravity-cli/settings.json" "antigrav"
check "$home/.gemini/antigravity-cli/antigravity-oauth-token" "antigrav"

echo
echo "$ok ok, $empty empty/placeholder, $missing missing"
[ "$missing" -eq 0 ] && [ "$empty" -eq 0 ]
