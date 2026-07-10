# shellcheck shell=bash
# Sourced from ~/.bashrc by postCreate.sh (see that script's "Devcontainer
# AI CLI home-config defaults" step). Kept in its own file rather than
# inlined as heredoc text there, so these functions are real, shellcheck-
# linted, independently testable bash (see
# test_devcontainer_agent_cli_shims.py) instead of an opaque string literal
# nothing else can see into. Every interactive shell opened in this
# container re-sources this file live via .bashrc, not a frozen copy baked
# in at postCreate.sh time -- editing this file and opening a new shell (or
# `source ~/.bashrc`) is enough to pick up a change, no rebuild required.

# Auto mode ("--permission-mode auto") biases Claude Code toward acting
# without stopping for clarifying questions. Default MCP additions to user
# scope so they stay under ~/.claude instead of the workspace.
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

# Keep OpenCode/Kilo plugin installs in their home-backed global config.
# Both CLIs already use XDG home paths for normal config/data/state, but
# their plugin command defaults to project-local config unless --global is
# supplied. A prior version of this shadowed opencode/kilo with wrapper
# *scripts* dropped in ~/.local/bin, relying on PATH order to intercept
# calls -- confirmed in practice to never fire, since remoteEnv.PATH (see
# devcontainer.json) never adds ~/.local/bin, and the base image's own
# default PATH puts it after /usr/local/bin (opencode's real binary) and
# ~/.local/share/pnpm/bin (kilo's real binary). Shell functions, like
# claude's above, are looked up before PATH regardless of ordering, so they
# don't have that problem.
#
# opencode and kilo need the exact same shim -- only the real binary's path
# differs -- so that logic lives once here rather than copied per-CLI; claude
# above stays separate rather than being forced into this same helper: its
# trigger (mcp add/add-json), injected flag (--scope user, not --global),
# and unconditional --permission-mode auto prefix are all different enough
# that sharing would need more parameters than the duplication it'd remove.
_devcontainer_cli_default_global_plugin() {
  local real_bin="$1" command_name arg has_global=0
  shift
  if [ "$1" = "plugin" ] || [ "$1" = "plug" ]; then
    command_name="$1"
    shift
    for arg in "$@"; do
      case "$arg" in
        -g|--global) has_global=1 ;;
      esac
    done
    if [ "$has_global" -eq 0 ]; then
      "$real_bin" "$command_name" --global "$@"
      return
    fi
    "$real_bin" "$command_name" "$@"
    return
  fi
  "$real_bin" "$@"
}

unalias opencode 2>/dev/null || true
function opencode {
  _devcontainer_cli_default_global_plugin /usr/local/bin/opencode "$@"
}

# kilo itself is installed by postCreate.sh's `pnpm install -g` step, not
# here -- unlike npm, pnpm never symlinks global packages into the Node
# install it ran from, so it lands in pnpm's own global bin dir.
unalias kilo 2>/dev/null || true
function kilo {
  _devcontainer_cli_default_global_plugin /home/vscode/.local/share/pnpm/bin/kilo "$@"
}
