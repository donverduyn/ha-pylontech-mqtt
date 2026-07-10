#!/bin/sh
set -e

prepare_agent_sync() {
  sudo mkdir -p "$HOME/.agent-sync" || return $?
  sudo chown vscode:vscode "$HOME/.agent-sync"
}

disable_copilot_cli_auto_update() {
  marker=/etc/devcontainer-copilot-cli/auto-update
  if [ -f "$marker" ]; then
    sudo rm -f "$marker"
  fi
}

# The devcontainers/features copilot-cli feature has no auto-update option
# as of the pinned 1.1.3 feature metadata. It installs Copilot during image
# creation, then pays a postStart `copilot update` check on every start when
# this marker exists. Remove it here; intentional latest refreshes are handled
# by `make update-deps` and a rebuild.
disable_copilot_cli_auto_update

# /home/vscode/.agent-sync is a directory bind mount, staging just
# .claude.json (see config-files.txt and seedHostConfig.sh for
# why that one file alone still needs staging). Docker auto-creates its
# target as root before this script runs if it doesn't already exist in the
# base image, so it needs its ownership fixed before anything below can
# write into it.
prepare_agent_sync

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

# is_bind_mounted lives in lib/is-bind-mounted.sh, shared with
# syncConfigOut.sh -- sourced first since sync-config-in.sh's
# sync_config_in() below calls it without defining or sourcing it itself.
# shellcheck disable=SC1091 # path is repo-local and always present
. "$(dirname "$0")/lib/is-bind-mounted.sh"

# full_ownership_walk/default_content_for_relpath/write_placeholder/
# sync_config_in live in lib/sync-config-in.sh, not here -- kept sourceable
# on their own so tests can exercise them directly without triggering this
# script's own top-level installs/downloads/sudo calls (see that file).
# shellcheck disable=SC1091 # path is repo-local and always present
. "$(dirname "$0")/lib/sync-config-in.sh"

tool_versions_file="$script_dir/tool-versions.env"

if [ ! -f "$tool_versions_file" ]; then
  exit 1
fi
# shellcheck disable=SC1090 # path is repo-local and checked above
. "$tool_versions_file"
: "${CODEX_VERSION:?missing CODEX_VERSION in tool-versions.env}"
: "${KILO_VERSION:?missing KILO_VERSION in tool-versions.env}"
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
# ship a default placeholder baked into the image itself (the claude-code
# feature's own install step does this for .claude.json -- confirmed in
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

# Codex and Kilo versions are pinned in tool-versions.env. Rebuilds consume
# those exact versions; `make update-deps` is the explicit
# operation that contacts npm and moves the pins.
#
# --allow-build is required, not cosmetic: both packages fetch a
# platform-specific native binary via a postinstall script (visible above as
# the "Downloading ..." lines), and pnpm 11's build-script approval gate
# blocks those scripts by default. Without this flag the gate falls back to
# an interactive "Choose which packages to build" picker gated on
# stdin.isTTY (not on $CI), which hangs forever under devcontainer's
# postCreateCommand -- confirmed in practice. --allow-build approves them
# non-interactively regardless of whether a TTY is attached.
#
# The flag must be repeated once per package, not comma-joined: pnpm's CLI
# parser treats each `--allow-build=` occurrence as one literal allow-list
# entry and never splits on commas, so `--allow-build=a,b` registers a
# single bogus entry named "a,b" that matches neither real package --
# confirmed in practice via the resulting global pnpm-workspace.yaml
# (`allowBuilds: {'a,b': true}`). @openai/codex has no gated postinstall so
# it installs fine either way, which is why only @kilocode/cli's build
# picker was ever seen hanging.
pnpm add -g \
  --config.minimumReleaseAge=0 \
  --allow-build=@openai/codex \
  --allow-build=@kilocode/cli \
  "@openai/codex@$CODEX_VERSION" \
  "@kilocode/cli@$KILO_VERSION"

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

# The claude/opencode/kilo shell function shims themselves live in
# lib/agent-cli-shims.sh, not inlined here as heredoc text -- that keeps
# them real, shellcheck-linted, independently testable bash instead of an
# opaque string literal only this script ever sees (see that file). This
# step's only job is making sure a future interactive shell sources it.
grep -qF '# Devcontainer AI CLI home-config defaults' /home/vscode/.bashrc || cat >> /home/vscode/.bashrc <<EOF

# Devcontainer AI CLI home-config defaults
. "$script_dir_abs/lib/agent-cli-shims.sh"
EOF

# Same idea for the Antigravity CLI (agy): --dangerously-skip-permissions
# auto-approves every tool permission request instead of prompting. A plain
# alias still lets any extra arguments you type pass through untouched
# (`agy foo` expands to `agy --dangerously-skip-permissions foo`).
grep -qF 'alias agy=' /home/vscode/.bashrc || echo "alias agy='agy --dangerously-skip-permissions'" >> /home/vscode/.bashrc
