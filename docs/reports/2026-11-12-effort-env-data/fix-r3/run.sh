#!/bin/bash
# Fix round 3 test runner: run.sh LABEL [pytest args...]
# Writes fix-r3/LABEL.log with the exact command, UTC start/end and exit code.
set -u
label=$1; shift
here=$(cd "$(dirname "$0")" && pwd)
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
export UV_CACHE_DIR="$TMPDIR/uv-cache"
unset PYTHONPATH
scratch="$TMPDIR/sf-eff-fix3"
case "$scratch" in *sf-eff-fix3*) ;; *) echo "bad scratch" >&2; exit 2;; esac
basetemp="$scratch/pytest-$label-$(date -u +%H%M%S)"
log="$here/$label.log"
cmd=(uv run --no-sync --with 'pytest-xdist>=3.6' pytest --basetemp="$basetemp" -p no:cacheprovider "$@")
{
  echo "# label: $label"
  echo "# start: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '# command:'; printf ' %q' "${cmd[@]}"; echo
} > "$log"
PYTHONDONTWRITEBYTECODE=1 "${cmd[@]}" >> "$log" 2>&1
rc=$?
{
  echo "# end: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# exit: $rc"
} >> "$log"
tail -n 25 "$log"
exit $rc
