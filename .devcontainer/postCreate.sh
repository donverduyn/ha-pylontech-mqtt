#!/bin/sh
set -e

# /home/vscode/.agent-sync is the one remaining bind mount (a directory, not
# a file — see seedHostAgentConfig.sh for why that distinction matters).
# Docker auto-creates its target as root before this script runs if it
# doesn't already exist in the base image, so it needs its ownership fixed
# before anything below can write into it.
sudo mkdir -p "$HOME/.agent-sync"
sudo chown vscode:vscode "$HOME/.agent-sync"

# Copy each AI CLI's staged config from the sync mount into its real
# container-local path. Plain copies, not mounts, into paths nothing else
# ever bind-mounts — so unlike the old per-file bind mounts, there's no
# Docker-auto-created-as-root parent dir to fix here; mkdir -p below creates
# each one as the vscode user directly.
while IFS='|' read -r relpath _kind; do
  case "$relpath" in
    ''|'#'*) continue ;;
  esac
  src="$HOME/.agent-sync/$relpath"
  dest="$HOME/$relpath"
  [ -f "$src" ] || continue
  mkdir -p "$(dirname "$dest")"
  cp -p "$src" "$dest"
done < "$(dirname "$0")/agent-config-files.txt"

# Whole-directory mount (not per-file), so its contents need to be usable too.
sudo chown -R vscode:vscode "$HOME/.copilot" || true

# xdg-utils provides xdg-open, which opencode/other CLIs shell out to for browser-based
# auth flows; without it, browser launches silently fail even though $BROWSER is set.
sudo apt-get update
sudo apt-get install -y xdg-utils

npm install -g @openai/codex @kilocode/cli

# /usr/local's site-packages is root-owned, so deps can't install into the base image's
# system Python as the vscode user. Use a uv-managed venv instead: uv is fast enough that
# recreating it on every container create isn't the bottleneck a plain pip venv was.
# The venv lives outside the bind-mounted workspace (in the container's own filesystem)
# so uv can hardlink from its cache instead of falling back to a full copy, and so every
# Python import at runtime isn't paying bind-mount I/O overhead.
# Installs from the same hash-pinned lock file CI uses (requirements_dev.lock.txt),
# not the loose requirements_dev.txt it's compiled from — otherwise the devcontainer
# silently drifts onto whatever's newest on PyPI (including newer Home Assistant
# releases than CI tests against) while CI stays pinned.
#
# --python 3.13 is required, not a default: despite the "3-3.13-trixie" tag, the
# base image's own build ships a second, newer Python (3.14 as of this writing)
# at /usr/local/bin — not from apt (dpkg doesn't know it), installed independently
# of the versioned apt package at /usr/bin/python3.13 the tag actually refers to.
# uv prefers that /usr/local one when unpinned, so the venv silently ends up on
# whatever that happens to be — and the lock file's pinned pydantic-core has no
# 3.14 wheel yet, so --require-hashes fails to build it and installs an unpinned
# newer one instead, same drift as above but for the interpreter and a dependency
# at once. This must track the base image tag and CI's actions/setup-python
# version above.
uv venv --python 3.13 /home/vscode/.venv
uv pip install --python /home/vscode/.venv/bin/python --require-hashes -r requirements_dev.lock.txt

# containerEnv/remoteEnv set PATH for processes VS Code itself launches, but a login shell
# (bash -l) re-sources /etc/profile, which unconditionally resets PATH and wipes that out.
# Debian sources /etc/profile.d/*.sh at the very end of /etc/profile, after that reset, so
# dropping the venv PATH there is what makes it survive in a plain terminal too.
sudo tee /etc/profile.d/00-venv.sh > /dev/null <<'EOF'
export VIRTUAL_ENV=/home/vscode/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
EOF

# Lets locally-installed npm CLI tools (e.g. from devDependencies) run by name from an
# interactive shell without npx/npm run. Deliberately only in .bashrc, not devcontainer.json's
# remoteEnv or /etc/profile.d: PATH resolves "./node_modules/.bin" relative to cwd on every
# lookup, so putting it in a shell rc keeps the risk scoped to interactive terminals the
# user opens, not every process VS Code spawns in every directory.
# shellcheck disable=SC2016 # $PATH must stay literal here — it's expanded later when .bashrc is sourced, not now
grep -qF 'node_modules/.bin' /home/vscode/.bashrc || echo 'export PATH="./node_modules/.bin:$PATH"' >> /home/vscode/.bashrc

# Auto mode ("--permission-mode auto") biases Claude Code toward acting
# without stopping for clarifying questions. settings.json's
# permissions.defaultMode is the documented way to default into a mode, but
# it's user-scoped only (project settings are ignored for this key) and has
# a known bug where it doesn't reliably activate on session start
# (anthropics/claude-code#49273) — an alias is the reliable way to make this
# devcontainer's Claude Code sessions start in auto mode by default.
grep -qF 'alias claude=' /home/vscode/.bashrc || echo "alias claude='claude --permission-mode auto'" >> /home/vscode/.bashrc
