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

# Start the sync-out watcher as early as this script can possibly manage --
# its only prerequisites are $HOME/.agent-sync existing and writable (just
# above) and inotifywait being on PATH, which the apt-packages feature in
# devcontainer.json now guarantees before this script even starts (see that
# feature entry for why). Every step below this point (the History symlink,
# the config copy-in loop, npm/uv installs) can still fail without taking the
# watcher down with it: devcontainer.json sets postStartCommand to waitFor
# postCreateCommand, so if this script exits non-zero, postStartCommand never
# runs at all for the rest of this container's life -- confirmed in practice,
# not just in theory: the devcontainers CLI logs "Skipping any further
# user-provided commands" and skips postStartCommand outright on a
# postCreateCommand failure. Any Claude Code login done in that broken
# container would then live only in the container's ephemeral filesystem and
# be discarded on the next rebuild, forcing a re-login, since nothing ever
# pushed it out to the host-backed .agent-sync mount. Safe to invoke twice:
# syncConfigOut.sh guards itself with a pidfile, so postStartCommand's later
# invocation of the same script is just a no-op once this one is already
# running.
#
# setsid, not just nohup: postCreateCommand runs as its own exec session
# same as postStartCommand does, and nohup alone only blocks SIGHUP -- a
# group-wide signal on that session's teardown could still take a bare
# nohup'd child down with it (see devcontainer.json's postStartCommand for
# where this was confirmed live). setsid detaches into a new session/process
# group so nothing that only signals the old group can reach it.
setsid nohup bash "$script_dir/syncConfigOut.sh" > /tmp/sync-config-out.log 2>&1 < /dev/null &

# Mounted from this project's host-side sync directory so VS Code Local
# History survives devcontainer rebuilds. Docker can create bind mount
# targets as root, so normalize ownership before the server writes to it.
# sudo mkdir -p "$HOME/.vscode-server/data/User/History"
# sudo chown -R vscode:vscode "$HOME/.vscode-server/data/User/History"

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


prepare_config_dir() {
  config_relpath=$1
  sudo mkdir -p "$HOME/$config_relpath" || return $?

  # seedHostConfig.sh drops this marker (inside the seeded tree itself, so it
  # rides the same bind mount into the container) the one time it populates
  # a "dir" config path fresh from the host. That's the only moment content
  # with a foreign (host) UID can exist here, so it's the only moment a
  # chown is needed: this mount is `fakeowner`-typed (Docker Desktop for
  # Mac's VM-level bind mount) -- a file nobody has ever explicitly chowned
  # shows each caller as its own owner by default, and an explicit chown
  # persists as real, caller-independent ownership from then on. Once this
  # walk has run, everything already present is really vscode-owned, and
  # everything written after this point is created by the vscode process
  # itself inside the container, so it's vscode-owned from birth regardless
  # -- there's nothing left for a later rebuild to fix, named-file or not.
  fresh_marker="$HOME/$config_relpath/.devcontainer-freshly-seeded"
  if [ -e "$fresh_marker" ]; then
    full_ownership_walk "$config_relpath"
    sudo rm -f "$fresh_marker"
  fi
}

copy_staged_json_config() {
  json_relpath=$1
  json_src="$HOME/.agent-sync/$json_relpath"
  json_dest="$HOME/$json_relpath"
  if [ ! -f "$json_src" ]; then
    return 0
  fi
  mkdir -p "$(dirname "$json_dest")" || return $?
  cp -p "$json_src" "$json_dest"
}

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

# Every "dir"-kind path in config-files.txt is its own live bind mount
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
      prepare_config_dir "$relpath"
      ;;
    json)
      copy_staged_json_config "$relpath"
      ;;
  esac
done < "$script_dir/config-files.txt"

# actionlint/hadolint versions+checksums are pinned in tool-versions.env --
# the same source tests.yaml's meta-lint job installs them from (see that
# job's identical curl+checksum steps) -- kept in sync by `make
# update-deps` instead of two independently hand-maintained version/hash
# pairs. Installed to /usr/local/bin rather than the .local/bin used for
# opencode/kilo below: that path isn't guaranteed on PATH for every
# shell/tool-invocation context (only a login shell's ~/.profile default
# adds it), while /usr/local/bin always is -- and .pre-commit-config.yaml's
# local/language:system hooks for both linters need to resolve them
# regardless of how pre-commit itself gets invoked.
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
# kilo itself is installed by `pnpm install -g` below into pnpm's own global
# bin dir, not nvm's -- unlike npm, pnpm never symlinks global packages into
# the Node install it ran from.
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
    exec /home/vscode/.local/share/pnpm/bin/kilo "$command_name" --global "$@"
  fi
  exec /home/vscode/.local/share/pnpm/bin/kilo "$command_name" "$@"
fi
exec /home/vscode/.local/share/pnpm/bin/kilo "$@"
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
