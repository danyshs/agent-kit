#!/usr/bin/env bash
# Lint gate for every language in the kit; any finding fails. Each check prints its file count,
# and a check that finds no files fails, since it proved nothing. Run from anywhere: ./lint.sh.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
# deliberately bad code the evals grade reviewers on: not ours to lint
EXCLUDE='^tools/evals/(skills|planted|runs|hidden|value)/'
failed=0

# tracked files matching the globs, minus EXCLUDE, one per line
files() { git ls-files -- "$@" | grep -Ev "$EXCLUDE" || true; }

# check NAME COUNT CMD...: runs CMD, shows its tail on failure
check() {
  local name=$1 count=$2 out status
  shift 2
  if [ "$count" -eq 0 ]; then
    echo "lint: $name: FAIL, no files (did the scope shrink?)"
    failed=1
    return
  fi
  out=$("$@" 2>&1)
  status=$?
  if [ "$status" -ne 0 ]; then
    printf '%s\n' "$out" | tail -n 40
    echo "lint: $name: FAIL ($count files)"
    failed=1
  else
    echo "lint: $name: ok ($count files)"
  fi
}

# fish has no linter; parse-check and format-check every file
# shellcheck disable=SC2329  # called by name through check below
fish_all() {
  local f bad=0
  for f in "$@"; do
    fish --no-execute "$f" || bad=1
    fish_indent --check "$f" || { echo "not fish_indent-clean: $f"; bad=1; }
  done
  return "$bad"
}

for tool in ruff pyright shellcheck fish fish_indent; do
  command -v "$tool" >/dev/null 2>&1 || { echo "lint: missing $tool"; exit 1; }
done

py=()
while IFS= read -r f; do py+=("$f"); done < <(files '*.py')
check ruff "${#py[@]}" ruff check "${py[@]}"
check "ruff format" "${#py[@]}" ruff format --check "${py[@]}"
check "pyright plugins" "$(files 'plugins/*.py' | wc -l | tr -d ' ')" pyright --warnings
check "pyright trajectory" "$(files 'tools/trajectory/*.py' | wc -l | tr -d ' ')" \
  pyright --warnings -p tools/trajectory
check "pyright evals" "$(files 'tools/evals/*.py' | wc -l | tr -d ' ')" \
  pyright --warnings -p tools/evals

# shell scripts have no extension or .sh; find them by their #! line
sh=()
while IFS= read -r f; do
  head -n 1 "$f" | grep -Eq '^#!(/bin/sh|/usr/bin/env (ba)?sh)' && sh+=("$f")
done < <(files)
check shellcheck "${#sh[@]}" shellcheck -S style "${sh[@]}"

fi=()
while IFS= read -r f; do
  head -n 1 "$f" | grep -Eq '^#!/usr/bin/env fish' && fi+=("$f")
done < <(files)
while IFS= read -r f; do fi+=("$f"); done < <(files '*.fish')
check fish "${#fi[@]}" fish_all "${fi[@]}"

[ "$failed" -eq 0 ] && echo "lint: clean"
exit "$failed"
