#!/usr/bin/env bash
# Assert the EvalShift CLI command surface that scripts/evalshift_action.py
# shells out to still exists. Assumes `evalshift` is already installed and on
# PATH (ci.yml installs the exact version pinned in action.yml first).
#
# The unit tests fake the CLI, so nothing else notices when a new EvalShift
# release renames a flag. No API keys, no model credits.
set -euo pipefail

# rich colorizes help on CI runners and styles the two leading dashes as
# separate spans, so a literal "--yes" never appears in the bytes. Ask for
# plain output, and strip any escapes that survive anyway.
export NO_COLOR=1
export COLUMNS="${COLUMNS:-200}"

echo "installed: $(evalshift --version 2>&1)"

check() {
  local subcommand="$1"
  shift
  local help missing=()
  local esc
  esc="$(printf '\033')"
  # Capture stderr too: rich renders help to whichever stream it picks, and a
  # silently empty capture would look like a rename.
  help="$(evalshift "$subcommand" --help 2>&1 | sed -E "s/${esc}\[[0-9;]*m//g")"
  for flag in "$@"; do
    grep -q -- "$flag" <<<"$help" || missing+=("$flag")
  done
  if (( ${#missing[@]} > 0 )); then
    echo "::error::'evalshift $subcommand' no longer accepts: ${missing[*]} — scripts/evalshift_action.py will break"
    echo "--- captured 'evalshift $subcommand --help' output ---"
    printf '%s\n' "$help"
    echo "--- end ---"
    return 1
  fi
  echo "evalshift $subcommand: $* all present"
}

check all --yes --config --suite
check push --no-create-project --config --suite
