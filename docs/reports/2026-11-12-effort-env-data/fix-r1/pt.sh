#!/bin/zsh
# pt.sh <label> [-n N] <pytest args...>: one recorded pytest run for fix r1 (sf-eff-f1).
# Records uptime at start and end, a private basetemp whose name contains "pytest",
# and the full log under $SCR/logs/<label>.log.
set -u
SCR=/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/sf-eff-f1
WT=/Users/maxghenis/.subfleet/worktrees/20261114-190051-eff-fix-r1
label=$1; shift
workers=""
if [[ ${1:-} == "-n" ]]; then workers=$2; shift 2; fi
mkdir -p $SCR/logs
log=$SCR/logs/$label.log
base=$SCR/pytest-$label
[[ -e $base ]] && { echo "basetemp $base exists; pick a new label"; exit 3; }
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
export UV_CACHE_DIR="${TMPDIR%/}/uv-cache" UV_PROJECT_ENVIRONMENT=$SCR/venv PYTHONDONTWRITEBYTECODE=1 HYPOTHESIS_STORAGE_DIRECTORY=$SCR/hypothesis
unset PYTHONPATH
export SUBFLEET_V1_RUNNER=$SCR/no-such-v1-runner   # never the real v1 tree
cd $WT
{
  echo "label: $label"; echo "start: $(date -u +%FT%TZ) load: $(uptime)"
  echo "args: $*"; echo "workers: ${workers:-none}"
} | tee $log
if [[ -n $workers ]]; then
  timeout 560 uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -p no:cacheprovider -p sf_eff_f1_safety --basetemp=$base -n $workers "$@" >> $log 2>&1
else
  timeout 560 $SCR/venv/bin/python -m pytest -p no:cacheprovider -p sf_eff_f1_safety --basetemp=$base "$@" >> $log 2>&1
fi
rc=$?
{ echo "end: $(date -u +%FT%TZ) load: $(uptime)"; echo "exit: $rc"; } | tee -a $log
tail -n 4 $log | head -n 2
exit $rc
