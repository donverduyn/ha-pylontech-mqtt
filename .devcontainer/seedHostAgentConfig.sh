#!/bin/sh
# Runs on the HOST (via devcontainer.json's initializeCommand), before the
# container exists — not inside it. devcontainer.json bind-mounts each path
# below into the container so every AI CLI's login/config survives rebuilds
# instead of re-provisioning from scratch every time.
#
# Docker's bind-mount, when the host side doesn't exist yet, creates a
# directory there rather than erroring — which silently breaks any mount
# that's supposed to be a *file* (auth.json, config.toml, ...). This script
# pre-creates each host-side file (with an empty-but-valid placeholder) so
# first-run bind-mounts always attach to a file, never an accidental
# directory. Once mounted, the CLI's own first login/config write goes
# straight through to the host — nothing further needs copying.
set -e

home="${HOME:-$USERPROFILE}"

seed_json() {
  [ -f "$1" ] || { mkdir -p "$(dirname "$1")" && printf '{}' > "$1"; }
}

seed_empty() {
  [ -f "$1" ] || { mkdir -p "$(dirname "$1")" && : > "$1"; }
}

seed_dir() {
  mkdir -p "$1"
}

# Claude Code
seed_json "$home/.claude/settings.json"
seed_json "$home/.claude/.credentials.json"
seed_json "$home/.claude.json"

# Codex
seed_json "$home/.codex/auth.json"
seed_empty "$home/.codex/config.toml"

# OpenCode
seed_json "$home/.config/opencode/opencode.jsonc"
seed_json "$home/.local/share/opencode/auth.json"

# Kilocode CLI
seed_json "$home/.config/kilo/kilo.jsonc"

# GitHub CLI (YAML; an empty file parses as "no config", same as absent)
seed_empty "$home/.config/gh/hosts.yml"
seed_empty "$home/.config/gh/config.yml"

# GitHub Copilot CLI — whole dir, not just a file: its login lives inside
# session-store.db (SQLite), not a plain config file, so the directory
# itself is the unit that needs to persist.
seed_dir "$home/.copilot"

# Antigravity CLI / Gemini CLI (shared ~/.gemini tree)
seed_json "$home/.gemini/oauth_creds.json"
seed_json "$home/.gemini/google_accounts.json"
seed_json "$home/.gemini/settings.json"
seed_json "$home/.gemini/trustedFolders.json"
seed_json "$home/.gemini/antigravity-cli/settings.json"
seed_empty "$home/.gemini/antigravity-cli/antigravity-oauth-token"
