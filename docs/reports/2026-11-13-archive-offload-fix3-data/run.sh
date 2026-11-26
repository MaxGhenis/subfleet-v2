#!/bin/zsh
# usage: run.sh <label> <timeout-seconds> [pytest args...]
# Fix round 3's runner, modeled on round 2's: one pytest call through the
# round's harness (sf_ao_scope, copied to $S/plugin), a new basetemp each run,
# uptime and free disk recorded at the start, uptime at the end.
set -u
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
S=$TMPDIR/sf-ao-fix3
label=$1; limit=$2; shift 2
export UV_CACHE_DIR=$TMPDIR/uv-cache
export SF_AO_FIX3_SCRATCH=$S/run
export PYTHONPATH=$S/plugin
cd /Users/maxghenis/.subfleet/worktrees/20261125-232725-ao-fix-r3
stamp=$(date -u +%H%M%S)
bt=$S/bt/pytest-$label-$stamp
mkdir -p $S/bt $S/logs $S/run
log=$S/logs/$label-$stamp.log
avail=$(df -k / | awk 'NR==2 {print $4}')
{
  echo "label $label start $(date -u +%T) bound ${limit}s basetemp $bt"
  echo "uptime-start: $(uptime)"
  df -H /
} > $log
if (( avail < 60000000 )); then
  echo "refused: free disk under 60 GB" >> $log
  cat $log
  exit 3
fi
echo "args: $*" >> $log
timeout $limit uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -p no:cacheprovider -p sf_ao_scope \
  --timeout 1200 --basetemp=$bt "$@" >> $log 2>&1
rc=$?
echo "rc=$rc end $(date -u +%T)" >> $log
echo "uptime-end: $(uptime)" >> $log
echo "log $log"
tail -45 $log
