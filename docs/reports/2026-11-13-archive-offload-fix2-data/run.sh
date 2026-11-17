#!/bin/zsh
# usage: run.sh <label> <timeout-seconds> [pytest args...]
set -u
export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR)"
S=$TMPDIR/sf-ao-fix2
label=$1; limit=$2; shift 2
export UV_CACHE_DIR=$TMPDIR/uv-cache
export SF_AO_FIX2_SCRATCH=$S/run
export PYTHONPATH=$S/plugin
cd /Users/maxghenis/.subfleet/worktrees/20261117-114947-ao-fix-r2
stamp=$(date -u +%H%M%S)
bt=$S/bt/pytest-$label-$stamp
mkdir -p $S/bt $S/logs
log=$S/logs/$label-$stamp.log
echo "basetemp $bt log $log start $(date -u +%T)"
timeout $limit uv run --no-sync --with 'pytest-xdist>=3.6' python -m pytest -p sf_ao_scope --basetemp=$bt "$@" > $log 2>&1
rc=$?
echo "rc=$rc end $(date -u +%T)"
tail -40 $log
