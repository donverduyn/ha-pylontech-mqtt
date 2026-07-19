#!/bin/sh
set -e

prepare_agent_sync() {
  sudo mkdir -p "$HOME/.agent-sync" || return $?
  sudo chown vscode:vscode "$HOME/.agent-sync"
}

# /home/vscode/.agent-sync is a directory bind mount, staging just
# .claude.json (see config-files.txt and seedHostConfig.sh for
# why that one file alone still needs staging). Docker auto-creates its
# target as root before this script runs if it doesn't already exist in the
# base image, so it needs its ownership fixed before anything below can
# write into it.
prepare_agent_sync

# Same root-owned-ancestor problem as .agent-sync above, but for the shared
# XDG-style parents of several mount targets: Docker auto-creates a bind
# mount target's whole ancestor chain as root, and confirmed against the
# base image (mcr.microsoft.com/devcontainers/python:3-3.14-trixie), only
# ~/.config pre-exists there, vscode-owned -- ~/.local, ~/.local/share, and
# ~/.local/state do not exist at all. So mounting .local/share/{opencode,kilo}
# and .local/state/{opencode,kilo} (see devcontainer.json's "mounts") leaves
# all three of those *parents* root-owned; full_ownership_walk (utils/fs.sh)
# only fixes ownership inside each mount's own leaf target, never its
# parents. That silently breaks anything else that later tries to create a
# sibling as the vscode user -- confirmed in practice for `pnpm add -g`
# below trying to create ~/.local/share/pnpm/bin, whose EACCES would abort
# this whole script under `set -e` before the venv or any AI CLI ever got
# installed.
#
# .local/share/pnpm/store gets the same explicit treatment here, not just
# the ownership fix above: devcontainer.json mounts a named volume there
# (see its "mounts" comment) so `pnpm add -g` below reuses already-downloaded
# package content across rebuilds instead of re-fetching every AI CLI from
# the npm registry every time. Docker creates that mount point the same
# root-owned way if the volume is fresh and the path doesn't already exist
# in the image, so it needs the same mkdir+chown as its siblings rather than
# being left to the general .local/share fix above, which only reaches one
# level deep.
sudo mkdir -p "$HOME/.local/share" "$HOME/.local/state" "$HOME/.local/share/pnpm/store" || exit $?
sudo chown vscode:vscode "$HOME/.local" "$HOME/.local/share" "$HOME/.local/state" \
  "$HOME/.local/share/pnpm" "$HOME/.local/share/pnpm/store"

script_dir=$(dirname "$0")
# Absolute, unlike $script_dir above: this one gets written into .bashrc
# below, read back by a future interactive shell whose cwd has no relation
# to wherever postCreate.sh itself was invoked from.
script_dir_abs=$(cd "$script_dir" && pwd)

# Mounted from this project's host-side sync directory so VS Code Local
# History survives devcontainer rebuilds. Docker can create bind mount
# targets as root, so normalize ownership before the server writes to it.
# sudo mkdir -p "$HOME/.vscode-server/data/User/History"
# sudo chown -R vscode:vscode "$HOME/.vscode-server/data/User/History"

# is_bind_mounted/full_ownership_walk/sync_config_in live in utils/fs.sh
# (shared with syncConfigOut.sh, which needs is_bind_mounted too), not here
# -- kept sourceable on its own so tests can exercise sync_config_in
# directly without triggering this script's own top-level
# installs/downloads/sudo calls (see that file).
# shellcheck disable=SC1091 # path is repo-local and always present
. "$(dirname "$0")/utils/fs.sh"

tool_versions_file="$script_dir/tool-versions.env"

if [ ! -f "$tool_versions_file" ]; then
  exit 1
fi
# shellcheck disable=SC1090 # path is repo-local and checked above
. "$tool_versions_file"
: "${CLAUDE_CODE_VERSION:?missing CLAUDE_CODE_VERSION in tool-versions.env}"
: "${CODEX_VERSION:?missing CODEX_VERSION in tool-versions.env}"
: "${COPILOT_CLI_VERSION:?missing COPILOT_CLI_VERSION in tool-versions.env}"
: "${KILO_VERSION:?missing KILO_VERSION in tool-versions.env}"
: "${OPENCODE_VERSION:?missing OPENCODE_VERSION in tool-versions.env}"
: "${ACTIONLINT_VERSION:?missing ACTIONLINT_VERSION in tool-versions.env}"
: "${ACTIONLINT_SHA256:?missing ACTIONLINT_SHA256 in tool-versions.env}"
: "${HADOLINT_VERSION:?missing HADOLINT_VERSION in tool-versions.env}"
: "${HADOLINT_SHA256:?missing HADOLINT_SHA256 in tool-versions.env}"

# Bind-mounted (see devcontainer.json's "mounts") to a path outside
# .vscode-server so Docker never has to auto-create .vscode-server itself as
# root — that would block the remote server's own install step (which runs
# before this script, as the vscode user) from creating .vscode-server/bin,
# a permission-denied error confirmed in practice. By now the server has
# already created .vscode-server/data/User itself, owned by vscode, so
# symlinking History in here is the only step left to make it persistent.
# mkdir -p "$HOME/.vscode-server/data/User"
# rm -rf "$HOME/.vscode-server/data/User/History"
# ln -s "$HOME/.local-history-sync" "$HOME/.vscode-server/data/User/History"

# Most paths in config-files.txt are their own live bind mount straight
# onto their real container path (see devcontainer.json's "mounts"), so
# there's nothing to copy in for those — the mount already *is* the real
# path. Docker still auto-creates each one as root before this script runs,
# same as .agent-sync above, so ownership needs fixing regardless. Which
# paths those are is checked live via sync_config_in's is_bind_mounted,
# not read from this file — see config-files.txt's header for why.
while IFS= read -r relpath; do
  case "$relpath" in
    ''|'#'*) continue ;;
  esac
  sync_config_in "$relpath"
done < "$script_dir/config-files.txt"

# Start the sync-out watcher only now, after the copy-in loop above has run
# sync_config_in for every path in config-files.txt -- not "as early as
# possible" like a prior version of this script did. Any of those paths can
# ship a default placeholder baked into the image itself (a previously used
# claude-code feature did this for .claude.json -- confirmed in
# practice: `docker run --rm <this image> stat /home/vscode/.claude.json`
# shows it present with a build-time mtime, before any postCreate.sh code
# ever runs -- and it's present again on every rebuild that reuses that
# cached image layer). Backgrounding the watcher any earlier than this
# raced its own one-time startup catch-up against the copy-in loop above:
# whichever won decided whether the host's real, previously-synced content
# survived the rebuild or got overwritten by the image's placeholder --
# confirmed in practice, this is what was silently discarding logins on
# rebuild. Now every unmounted path is already the real, restored copy by
# the time the watcher takes its first look, so there's nothing stale left
# for it to push out.
#
# Still runs before the slower, more failure-prone steps below (actionlint/
# hadolint downloads, pnpm/uv installs): if any of those fail, postStartCommand
# never runs at all (devcontainer.json waits for postCreateCommand, and the
# devcontainers CLI skips postStartCommand outright on a postCreateCommand
# failure), so launching the watcher here instead of at the very end still
# means a login made in an otherwise-broken container gets synced out.
#
# setsid, not just nohup: postCreateCommand runs as its own exec session
# same as postStartCommand does, and nohup alone only blocks SIGHUP -- a
# group-wide signal on that session's teardown could still take a bare
# nohup'd child down with it (see devcontainer.json's postStartCommand for
# where this was confirmed live). setsid detaches into a new session/process
# group so nothing that only signals the old group can reach it.
setsid nohup bash "$script_dir/syncConfigOut.sh" > /tmp/sync-config-out.log 2>&1 < /dev/null &

# actionlint/hadolint versions+checksums are pinned in tool-versions.env --
# the same source tests.yaml's meta-lint job installs them from (see that
# job's identical curl+checksum steps) -- kept in sync by `make
# update-deps` instead of two independently hand-maintained version/hash
# pairs. Installed to /usr/local/bin rather than ~/.local/bin: that path
# isn't guaranteed on PATH for every shell/tool-invocation context (only a
# login shell's ~/.profile default adds it), while /usr/local/bin always is
# -- and .pre-commit-config.yaml's local/language:system hooks for both
# linters need to resolve them regardless of how pre-commit itself gets
# invoked.
curl -sSfLo /tmp/actionlint.tar.gz \
  "https://github.com/rhysd/actionlint/releases/download/v$ACTIONLINT_VERSION/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz"
echo "$ACTIONLINT_SHA256  /tmp/actionlint.tar.gz" | sha256sum -c
tar xzf /tmp/actionlint.tar.gz -C /tmp actionlint
sudo install -m755 /tmp/actionlint /usr/local/bin/actionlint

curl -sSfLo /tmp/hadolint \
  "https://github.com/hadolint/hadolint/releases/download/v$HADOLINT_VERSION/hadolint-linux-x86_64"
echo "$HADOLINT_SHA256  /tmp/hadolint" | sha256sum -c
sudo install -m755 /tmp/hadolint /usr/local/bin/hadolint

# All npm-distributed AI CLIs are constrained to major release lines in
# tool-versions.env. Rebuilds resolve the newest compatible minor/patch;
# `make update-deps` explicitly contacts npm and moves the major constraints.
#
# --allow-build approves the packages with native-binary postinstall scripts
# through pnpm 11's otherwise interactive build-script gate. Repeat the flag
# once per package: pnpm treats each occurrence as one literal allow-list entry.
# Codex and Copilot do not currently declare gated postinstall scripts.
pnpm add -g \
  --config.minimumReleaseAge=0 \
  --allow-build=@openai/codex \
  --allow-build=@github/copilot \
  --allow-build=@anthropic-ai/claude-code \
  --allow-build=@kilocode/cli \
  --allow-build=opencode-ai \
  "@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION" \
  "@openai/codex@$CODEX_VERSION" \
  "@github/copilot@$COPILOT_CLI_VERSION" \
  "@kilocode/cli@$KILO_VERSION" \
  "opencode-ai@$OPENCODE_VERSION"

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
# --python 3.14 is required, not a default: despite the "3-3.14-trixie" tag, the
# base image's own build has shipped a second Python at /usr/local/bin in the
# past (not from apt — dpkg doesn't know it) alongside the versioned apt package
# the tag actually refers to. uv prefers that /usr/local one when unpinned, so
# the venv can silently end up on whatever that happens to be — and a lock
# file's pinned pydantic-core with no wheel for it would then fail to build
# under --require-hashes and install an unpinned newer one instead, same drift
# as above but for the interpreter and a dependency at once. This must track
# the base image tag and CI's actions/setup-python version above.
uv venv --python 3.14 /home/vscode/.venv
uv pip install --python /home/vscode/.venv/bin/python --require-hashes -r requirements_dev.lock.txt

# containerEnv/remoteEnv set PATH for processes VS Code itself launches, but a login shell
# (bash -l) re-sources /etc/profile, which unconditionally resets PATH and wipes that out.
# Debian sources /etc/profile.d/*.sh at the very end of /etc/profile, after that reset, so
# dropping the venv PATH there is what makes it survive in a plain terminal too.
sudo tee /etc/profile.d/00-venv.sh > /dev/null <<'EOF'
export VIRTUAL_ENV=/home/vscode/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
EOF

# Same login-shell problem as the venv above, but for pnpm's global bin dir:
# devcontainer.json's remoteEnv.PATH only reaches shells/processes VS Code
# itself launches, not a plain login shell re-sourcing /etc/profile. Without
# this, `codex`/`kilo` (installed via `pnpm install -g` above) resolve fine
# from a VS Code terminal but 404 from `bash -l` or an SSH session.
sudo tee /etc/profile.d/01-pnpm.sh > /dev/null <<'EOF'
export PATH="/home/vscode/.local/share/pnpm/bin:$PATH"
EOF

# Lets locally-installed npm CLI tools (e.g. from devDependencies) run by name from an
# interactive shell without npx/npm run. Deliberately only in .bashrc, not devcontainer.json's
# remoteEnv or /etc/profile.d: PATH resolves "./node_modules/.bin" relative to cwd on every
# lookup, so putting it in a shell rc keeps the risk scoped to interactive terminals the
# user opens, not every process VS Code spawns in every directory.
# shellcheck disable=SC2016 # $PATH must stay literal here — it's expanded later when .bashrc is sourced, not now
grep -qF 'node_modules/.bin' /home/vscode/.bashrc || echo 'export PATH="./node_modules/.bin:$PATH"' >> /home/vscode/.bashrc

# Same cwd-relative reasoning as node_modules/.bin above, for this repo's own
# dev tools: .devcontainer/bin/pylon_cli (a symlink to scripts/pylon_cli.py,
# the interactive BMS/stub console client) can then be run as a bare
# `pylon_cli` from a terminal opened at the repo root, without
# `python scripts/pylon_cli.py`.
# shellcheck disable=SC2016 # $PATH must stay literal here — it's expanded later when .bashrc is sourced, not now
grep -qF './.devcontainer/bin:' /home/vscode/.bashrc || echo 'export PATH="./.devcontainer/bin:$PATH"' >> /home/vscode/.bashrc

# utils/cli.sh's _devcontainer_define_cli_shim (see that file) is a generic
# factory: given a CLI name plus its trigger words, override flag, default
# flag, and any unconditional prefix flags, it defines a same-named shell
# function that applies them. The three calls below are the only
# CLI-specific knowledge that exists anywhere -- utils/cli.sh has no idea
# what claude, opencode, kilo, --scope, or --global are.
#
# claude: auto mode ("--permission-mode auto") biases Claude Code toward
# acting without stopping for clarifying questions, applied
# unconditionally. MCP additions default to user scope so they stay under
# ~/.claude instead of the workspace, unless -s/--scope was already given.
#
# opencode/kilo: both already use XDG home paths for normal config/data/
# state, but their plugin command defaults to project-local config unless
# --global is supplied -- defaulted here the same way, unless -g/--global
# was already given. All three binaries are installed by the `pnpm add -g`
# step above and live in pnpm's global bin directory.
#
# The calls below are plain data (CLI name, real binary path, trigger
# words, flags) -- no "$@" or other shell metacharacters -- so they're safe
# to write directly into this heredoc even though it's unquoted (needed so
# $script_dir_abs still expands into an absolute path baked into .bashrc,
# same as before). See _devcontainer_define_cli_shim's own comment for why
# that distinction matters.
grep -qF '# Devcontainer AI CLI home-config defaults' /home/vscode/.bashrc || cat >> /home/vscode/.bashrc <<EOF

# Devcontainer AI CLI home-config defaults
. "$script_dir_abs/utils/cli.sh"
_devcontainer_define_cli_shim claude /home/vscode/.local/share/pnpm/bin/claude "mcp add|mcp add-json" -s --scope "--scope user" "--permission-mode auto"
_devcontainer_define_cli_shim opencode /home/vscode/.local/share/pnpm/bin/opencode "plugin|plug" -g --global --global ""
_devcontainer_define_cli_shim kilo /home/vscode/.local/share/pnpm/bin/kilo "plugin|plug" -g --global --global ""
EOF

# Same idea for the Antigravity CLI (agy): --dangerously-skip-permissions
# auto-approves every tool permission request instead of prompting. A plain
# alias still lets any extra arguments you type pass through untouched
# (`agy foo` expands to `agy --dangerously-skip-permissions foo`).
grep -qF 'alias agy=' /home/vscode/.bashrc || echo "alias agy='agy --dangerously-skip-permissions'" >> /home/vscode/.bashrc
