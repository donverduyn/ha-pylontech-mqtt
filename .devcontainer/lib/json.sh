#!/bin/bash
# Converts JSONC (JSON with // line comments, /* block */ comments, and
# trailing commas -- what devcontainer.json actually is) into strict JSON on
# stdout, given a file path as $1. Pure bash, not awk/jq/python: bash is
# already the one thing seedHostConfig.sh unconditionally requires on the
# host (devcontainer.json's initializeCommand invokes it directly, not
# through its own #!/bin/sh shebang), so doing the parsing here adds no new
# tool the host has to have -- whereas awk, jq, or @devcontainers/cli would
# each be one more thing to assume is present on an arbitrary developer's
# machine, or (for @devcontainers/cli) can't even be installed there in time
# regardless, since initializeCommand runs before the devcontainer -- and
# anything it could install -- exists.
#
# A single character-at-a-time state machine, not a line-based comment
# strip: string literals are tracked explicitly (including \" escapes) so a
# "//" or a trailing comma that's part of a quoted string's actual content
# is never mistaken for a comment or touched -- the failure mode a naive
# `grep -v '^\s*//'` has no way to avoid.
#
# Trailing commas are handled by buffering: a "," found outside a string is
# held (never emitted immediately) instead of assuming it's real. Whitespace
# and comment text pass through without resolving that hold. The first real
# token seen after it decides which it was: "]" or "}" means it truly was a
# trailing comma, so it's dropped; anything else means it was a genuine
# separator, so it's emitted just before that token.
set -e

in_string=0
in_block_comment=0
pending_comma=0
out=""

process_line() {
  local line="$1"
  local n=${#line}
  local i=0 c c2
  while [ "$i" -lt "$n" ]; do
    c="${line:i:1}"
    c2="${line:i:2}"

    if [ "$in_block_comment" -eq 1 ]; then
      if [ "$c2" = "*/" ]; then in_block_comment=0; i=$((i + 2)); else i=$((i + 1)); fi
      continue
    fi

    if [ "$in_string" -eq 1 ]; then
      if [ "$pending_comma" -eq 1 ]; then out+=","; pending_comma=0; fi
      if [ "$c" = $'\\' ]; then
        out+="${line:i:2}"
        i=$((i + 2))
        continue
      fi
      if [ "$c" = '"' ]; then in_string=0; fi
      out+="$c"
      i=$((i + 1))
      continue
    fi

    if [ "$c2" = "//" ]; then break; fi # rest of the line is a line comment
    if [ "$c2" = "/*" ]; then
      in_block_comment=1
      i=$((i + 2))
      continue
    fi

    if [ "$c" = "," ]; then
      pending_comma=1
      i=$((i + 1))
      continue
    fi

    if [ "$c" = " " ] || [ "$c" = "$(printf '\t')" ]; then
      out+="$c"
      i=$((i + 1))
      continue
    fi

    if [ "$c" = "]" ] || [ "$c" = "}" ]; then
      pending_comma=0 # was a trailing comma -- drop it
      out+="$c"
      i=$((i + 1))
      continue
    fi

    if [ "$pending_comma" -eq 1 ]; then out+=","; pending_comma=0; fi
    if [ "$c" = '"' ]; then in_string=1; fi
    out+="$c"
    i=$((i + 1))
  done
}

while IFS= read -r line || [ -n "$line" ]; do
  process_line "$line"
  # A pending comma survives across the newline exactly like it survives
  # whitespace or a line comment, since the next real token hasn't been
  # seen yet -- resolved whenever it actually shows up, on this line or a
  # later one.
  out+=$'\n'
done < "$1"

printf '%s' "$out"
