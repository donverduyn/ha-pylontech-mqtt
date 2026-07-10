# shellcheck shell=bash
# Sourced from ~/.bashrc, before postCreate.sh's "Devcontainer AI CLI
# home-config defaults" step calls _devcontainer_define_cli_shim below to
# actually wire up claude/opencode/kilo (see that script). This file
# provides only the generic, product-agnostic mechanic: given a CLI's real
# binary, which leading words trigger a default flag, which flag counts as
# an explicit override, and what the default is, define a same-named shell
# function that applies it. It has no idea what claude, opencode, kilo,
# --scope, or --global are -- postCreate.sh supplies all of that as plain
# data at its own call site, not hidden in here. Kept as a real,
# independently testable file, linted like any other (see
# test_devcontainer_cli.py) rather than folded into postCreate.sh. Every
# interactive shell opened in this container re-sources this file live via
# .bashrc, not a frozen copy baked in at postCreate.sh time -- editing this
# file and opening a new shell (or `source ~/.bashrc`) is enough to pick up
# a change, no rebuild required.

# True if any of "$3.." is exactly $1, exactly $2, or $2 followed by
# "=value" (e.g. calling with -g --global matches both "-g" and
# "--global=true"). Pure arg-scanning mechanics -- deliberately knows
# nothing about which flags matter for which CLI or what to default them
# to; callers decide that and act on the true/false result themselves.
_devcontainer_args_has_flag() {
  local short="$1" long="$2" arg
  shift 2
  for arg in "$@"; do
    case "$arg" in
      "$short" | "$long" | "$long"=*) return 0 ;;
    esac
  done
  return 1
}

# Runs $1 (the real CLI binary), injecting $5 (a default flag) right after
# whichever leading words in "$7.." matched one of $2's alternatives --
# unless $3/$4 (an override flag) is already present anywhere in "$7..", or
# no alternative matched at all, in which case it's a plain passthrough.
# $6 (prefix flags) are prepended before the real binary's own args
# unconditionally, whether or not the trigger matched.
#
# Shared by every CLI shim regardless of its specific
# trigger/flags/default/prefix -- _devcontainer_define_cli_shim below just
# wires a name to a fixed set of these arguments. Never called directly by
# a shim; always through the function that generates.
#
# $1 real_bin       path to the real CLI binary
# $2 trigger_alts    "|"-separated alternatives, each a space-separated
#                    sequence of leading words that triggers the default
#                    (e.g. "mcp add|mcp add-json", or "plugin|plug")
# $3 override_short  short override flag (e.g. -s), or "" if none
# $4 override_long   long override flag (e.g. --scope), or "" if none
# $5 default_flag    space-separated flag(s) to inject right after the
#                    matched trigger words (e.g. "--scope user")
# $6 prefix_flags    space-separated flag(s) always prepended before the
#                    real binary's own args (e.g. "--permission-mode
#                    auto"), or "" if none
# $7.. the caller's actual arguments
_devcontainer_cli_shim_run() {
  local real_bin="$1" trigger_alts="$2" override_short="$3" override_long="$4" default_flag="$5" prefix_flags="$6"
  shift 6
  local -a args=("$@")

  local -a alternatives
  IFS='|' read -ra alternatives <<< "$trigger_alts"

  local alt matched=-1
  for alt in "${alternatives[@]}"; do
    local -a words
    read -ra words <<< "$alt"
    local n=${#words[@]} ok=1 i
    for ((i = 0; i < n; i++)); do
      if [ "${args[i]-}" != "${words[i]}" ]; then
        ok=0
        break
      fi
    done
    if [ "$ok" -eq 1 ]; then
      matched=$n
      break
    fi
  done

  if [ "$matched" -ge 0 ] && ! _devcontainer_args_has_flag "$override_short" "$override_long" "${args[@]}"; then
    # shellcheck disable=SC2086 # prefix_flags/default_flag are deliberately unquoted, space-separated flag lists
    "$real_bin" $prefix_flags "${args[@]:0:matched}" $default_flag "${args[@]:matched}"
    return
  fi

  # shellcheck disable=SC2086 # prefix_flags is a deliberately unquoted, space-separated flag list
  "$real_bin" $prefix_flags "${args[@]}"
}

# Defines a shell function named $1 that wraps $2 (the real binary),
# applying _devcontainer_cli_shim_run's default-flag-injection logic with
# $3.. baked in -- see that function for what each argument means.
#
# Generated via eval, rather than a fixed template that forwards "$@"
# itself, so that postCreate.sh's own heredoc (see that script) never has
# to write out a literal "$@" -- which its own (non-interactive) shell
# would expand immediately, into its own empty positional params, instead
# of leaving it for the real interactive shell that eventually sources the
# result. Every value substituted in below is single-quoted, trusted,
# literal data supplied by postCreate.sh itself (never user input), so
# there's no injection risk in building the function source this way.
_devcontainer_define_cli_shim() {
  local name="$1" real_bin="$2" trigger_alts="$3" override_short="$4" override_long="$5" default_flag="$6" prefix_flags="$7"
  eval "
unalias $name 2>/dev/null || true
function $name {
  _devcontainer_cli_shim_run '$real_bin' '$trigger_alts' '$override_short' '$override_long' '$default_flag' '$prefix_flags' \"\$@\"
}
"
}
