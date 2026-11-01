#!/bin/zsh
# hold.sh <name> <linked|resolved|worktree> <pytest args...>: one pytest under the suite lock.
# Writes <data>/holds/<name>.xml (junit), <name>.log (output) and appends a line to holds.tsv.
S="$(getconf DARWIN_USER_TEMP_DIR)sf-r272r13"
W=/Users/maxghenis/.subfleet/worktrees/20261031-170839-pr272-review-r13
DATA=$W/docs/reports/2026-10-31-retention-pins-review-r13-data
name=$1; variant=$2; shift 2
mkdir -p $DATA/holds $S/real $S/hyp
[[ -L $S/link ]] || ln -s $S/real $S/link
case $variant in
  linked) T=$S/link/hold-$name ;;                       # reached through /var and through $S/link
  resolved) T=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' $S/real)/hold-$name ;;  # no link at all
  worktree) T=$W/wt-$name ;;                            # inside the job worktree: no temp part in its name
  *) print -u2 "bad variant"; exit 64 ;;
esac
mkdir -p $T
start=$(date -u +%FT%TZ); load=$(sysctl -n vm.loadavg)
print -r -- "queued $start load $load TMPDIR=$T" > $DATA/holds/$name.log
cd $W
TMPDIR=$T/ HYPOTHESIS_STORAGE_DIRECTORY=$S/hyp PYTHONDONTWRITEBYTECODE=1 /bin/zsh /Users/maxghenis/reviews/subfleet-hub/host-runs/suite_hold.sh \
  env -u SUBFLEET_RETENTION_DORMANT -u PYTHONPATH UV_PROJECT_ENVIRONMENT=$S/venv UV_CACHE_DIR=$S/uv-cache \
  uv run --no-sync pytest -q -p no:cacheprovider --junitxml=$DATA/holds/$name.xml "$@" >> $DATA/holds/$name.log 2>&1
rc=$?
end=$(date -u +%FT%TZ)
summary=$(grep -E '^(=+ )?[0-9]+ (passed|failed|error|skipped|xfailed|deselected)|^=+ .*(passed|failed|error).* =+$|no tests ran' $DATA/holds/$name.log | tail -1)
print -r -- "$name	$variant	$start	$end	$load	rc=$rc	$summary	$*" >> $DATA/holds.tsv
python3 -c 'import shutil,sys; p=sys.argv[1]; assert "/sf-r272r13/" in p or "/wt-" in p, p; shutil.rmtree(p, ignore_errors=True)' $T
print -r -- "$name rc=$rc $start..$end load[$load] :: $summary"
tail -3 $DATA/holds/$name.log
exit $rc
