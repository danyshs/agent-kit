#!/usr/bin/env bash
# Lint gate for every language in the kit; any finding fails. Each check prints its file count,
# and a check that finds no files fails, since it proved nothing. Run from anywhere: ./lint.sh.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
# deliberately bad code the evals grade reviewers on: not ours to lint
EXCLUDE='^tools/evals/(skills|planted|runs|hidden|value)/'
failed=0

# tracked files matching the globs, minus EXCLUDE, one per line
# new files too: a file not yet added to git must not pass by being unseen
files() { git ls-files --cached --others --exclude-standard -- "$@" | grep -Ev "$EXCLUDE" || true; }

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

for tool in ruff pyright shellcheck uvx; do
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

# macOS /bin/sh is bash 3.2, which misparses things shellcheck passes (a case ) inside $( ))
# shellcheck disable=SC2329  # called by name through check below
sh_parse() {
  local f bad=0
  for f in "$@"; do
    if head -n 1 "$f" | grep -q bash; then /bin/bash -n "$f" || bad=1; else /bin/sh -n "$f" || bad=1; fi
  done
  return "$bad"
}
check "sh parse" "${#sh[@]}" sh_parse "${sh[@]}"

# guard.py and prices.json ship in two plugins; a fix to one copy must reach the other
copies=(guard.py prices.json)
# shellcheck disable=SC2329  # called by name through check below
copies_match() {
  local name bad=0
  for name in "${copies[@]}"; do
    cmp "plugins/guards/scripts/$name" "plugins/rhino-owl/scripts/$name" || bad=1
  done
  return "$bad"
}
check "copies match" "${#copies[@]}" copies_match

# every plugin's rule loads in every session at once; detail belongs in a skill
RULE_WORDS_MAX=1500
rules=()
while IFS= read -r f; do rules+=("$f"); done < <(files 'rules/*.md')
words=0
[ "${#rules[@]}" -gt 0 ] && words=$(cat "${rules[@]}" | wc -w | tr -d ' ')
check "rule words, $words of $RULE_WORDS_MAX" "${#rules[@]}" test "$words" -le "$RULE_WORDS_MAX"

# workflow files: actionlint also runs shellcheck on each run: block
wf=()
while IFS= read -r f; do wf+=("$f"); done < <(files '.github/workflows/*.yml')
check actionlint "${#wf[@]}" uvx --from actionlint-py==1.7.12.25 actionlint -color=false "${wf[@]}"

[ "$failed" -eq 0 ] && echo "lint: clean"
exit "$failed"
