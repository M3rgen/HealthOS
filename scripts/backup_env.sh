#!/usr/bin/env bash
# Shared backup settings reader; never execute the settings file.

file_value() {
  # Read settings without sourcing secrets. Compose comments in unquoted values
  # need preceding whitespace; a # inside quotes or inside a word is literal.
  [ -r "$1" ] || return 0
  awk -v key="$2" '
    match($0, "^[[:space:]]*(export[[:space:]]+)?" key "[[:space:]]*=") {
      raw = substr($0, RLENGTH + 1)
      value = raw
      sub(/^[[:space:]]+/, "", value)
      quote = substr(value, 1, 1)
      if (quote == "\047" || quote == "\042") {
        result = ""
        for (i = 2; i <= length(value); i++) {
          c = substr(value, i, 1)
          if (c == quote) break
          if (c == "\\" && substr(value, i + 1, 1) == quote) c = substr(value, ++i, 1)
          result = result c
        }
      } else {
        value = raw
        sub(/[[:space:]]+#.*$/, "", value)
        sub(/^[[:space:]]+/, "", value)
        sub(/[[:space:]]+$/, "", value)
        result = value
      }
      found = 1
    }
    END { if (found) printf "%s", result }
  ' "$1"
}
