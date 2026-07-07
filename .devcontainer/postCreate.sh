#!/bin/sh
set -e

# /home/vscode/.agent-sync is a directory bind mount, staging just
# .claude.json (see agent-config-files.txt and seedHostAgentConfig.sh for
# why that one file alone still needs staging). Docker auto-creates its
# target as root before this script runs if it doesn't already exist in the
# base image, so it needs its ownership fixed before anything below can
# write into it.
sudo mkdir -p "$HOME/.agent-sync"
sudo chown vscode:vscode "$HOME/.agent-sync"

# Every "dir"-kind path in agent-config-files.txt is its own live bind mount
# straight onto its real container path (see devcontainer.json's "mounts"),
# so there's nothing to copy in for those — the mount already *is* the real
# path. Docker still auto-creates each one as root before this script runs,
# same as .agent-sync above, so ownership needs fixing regardless. The one
# "json"-kind path (.claude.json) isn't mounted at all — copy it in from
# the staging mount instead.
while IFS='|' read -r relpath kind; do
  case "$relpath" in
    ''|'#'*) continue ;;
  esac
  case "$kind" in
    dir)
      sudo mkdir -p "$HOME/$relpath"
      sudo chown -R vscode:vscode "$HOME/$relpath"
      ;;
    json)
      src="$HOME/.agent-sync/$relpath"
      dest="$HOME/$relpath"
      [ -f "$src" ] || continue
      mkdir -p "$(dirname "$dest")"
      cp -p "$src" "$dest"
      ;;
  esac
done < "$(dirname "$0")/agent-config-files.txt"

# xdg-utils provides xdg-open, which opencode/other CLIs shell out to for browser-based
# auth flows; without it, browser launches silently fail even though $BROWSER is set.
# inotify-tools provides inotifywait, which syncAgentConfigOut.sh (postStartCommand)
# uses to push .claude.json out to the host the moment it changes instead of polling.
# (No mosquitto here: the e2e suite runs its broker as a pinned container —
# see docker/docker-compose.test.yml — via the docker-outside-of-docker
# feature, so no broker binary is needed in the devcontainer itself.)
sudo apt-get update
sudo apt-get install -y xdg-utils inotify-tools

npm install -g @openai/codex @kilocode/cli

# Keep OpenCode/Kilo plugin installs in their home-backed global config.
# Both CLIs already use XDG home paths for normal config/data/state, but their
# plugin command defaults to project-local config unless --global is supplied.
mkdir -p /home/vscode/.local/bin
cat > /home/vscode/.local/bin/opencode <<'EOF'
#!/bin/sh
if [ "$1" = "plugin" ] || [ "$1" = "plug" ]; then
  command_name="$1"
  shift
  has_global=0
  for arg in "$@"; do
    case "$arg" in
      -g|--global) has_global=1 ;;
    esac
  done
  if [ "$has_global" -eq 0 ]; then
    exec /usr/local/bin/opencode "$command_name" --global "$@"
  fi
  exec /usr/local/bin/opencode "$command_name" "$@"
fi
exec /usr/local/bin/opencode "$@"
EOF
cat > /home/vscode/.local/bin/kilo <<'EOF'
#!/bin/sh
if [ "$1" = "plugin" ] || [ "$1" = "plug" ]; then
  command_name="$1"
  shift
  has_global=0
  for arg in "$@"; do
    case "$arg" in
      -g|--global) has_global=1 ;;
    esac
  done
  if [ "$has_global" -eq 0 ]; then
    exec /usr/local/share/nvm/current/bin/kilo "$command_name" --global "$@"
  fi
  exec /usr/local/share/nvm/current/bin/kilo "$command_name" "$@"
fi
exec /usr/local/share/nvm/current/bin/kilo "$@"
EOF
chmod +x /home/vscode/.local/bin/opencode /home/vscode/.local/bin/kilo

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
# without stopping for clarifying questions. Default MCP additions to user
# scope so they stay under ~/.claude instead of the workspace.
grep -qF '# Devcontainer AI CLI home-config defaults' /home/vscode/.bashrc || cat >> /home/vscode/.bashrc <<'EOF'

# Devcontainer AI CLI home-config defaults
unalias claude 2>/dev/null || true
function claude {
  local real_claude=/home/vscode/.local/bin/claude
  if [ "$1" = "mcp" ] && { [ "$2" = "add" ] || [ "$2" = "add-json" ]; }; then
    local arg has_scope=0 subcommand
    for arg in "$@"; do
      case "$arg" in
        -s|--scope|--scope=*) has_scope=1 ;;
      esac
    done
    if [ "$has_scope" -eq 0 ]; then
      subcommand="$2"
      shift 2
      "$real_claude" --permission-mode auto mcp "$subcommand" --scope user "$@"
      return
    fi
  fi
  "$real_claude" --permission-mode auto "$@"
}
EOF

# Same idea for the Antigravity CLI (agy): --dangerously-skip-permissions
# auto-approves every tool permission request instead of prompting. A plain
# alias still lets any extra arguments you type pass through untouched
# (`agy foo` expands to `agy --dangerously-skip-permissions foo`).
grep -qF 'alias agy=' /home/vscode/.bashrc || echo "alias agy='agy --dangerously-skip-permissions'" >> /home/vscode/.bashrc
