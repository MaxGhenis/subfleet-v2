#!/bin/bash
# Fake subfleet for the fanout harness. Never touches the daemon. Replays the
# row shapes of `subfleet run --batch --json` and `subfleet wait --json` on
# main at 2441fbea (cli.py cmd_run_batch, wait_jobs).
# SCENARIO: comma list of refuse-b, submit-down, unknown-c, wait-down-first,
#   lost-c, failed-a, unknown-id, no-name
S=",${SCENARIO:-},"; C="$FAKE_STATE/calls.$1"; n=$(cat "$C" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$C"
echo "fake subfleet $*" >&2
case "$1" in
  run)
    shift; m=""; rid=""; detach=0; while [ $# -gt 0 ]; do case "$1" in --batch) m="$2"; shift;; --request-id) rid="$2"; shift;; -d) detach=1;; esac; shift; done
    [ "$detach" = 1 ] || { echo "fake: -d missing" >&2; exit 98; }
    SCEN="$S" RID="$rid" N="$n" python3 - "$m" <<'PY'
import json,sys,os
doc=json.load(open(sys.argv[1])); S=os.environ["SCEN"]; rid=os.environ["RID"] or "auto-uuid"; n=int(os.environ["N"]); worst=0
down = ",submit-down," in S and n == 1
for i,job in enumerate(doc['jobs'],1):
    row={"batch":rid,"label":doc['label'],"index":i,"name":job['name'],"workdir":job['workdir'],"request_id":f"{rid}-{i}"}
    if down: row.update(job_id=None, outcome="not-sent", rc=69, error="daemon unavailable: connection refused", fix="start the daemon and rerun the same manifest with the same --request-id"); worst=worst or 69
    elif ",refuse-b," in S and job['name']=='b': row.update(job_id=None, outcome="refused", rc=7, error="output path is held by another job", fix="use a different -o path"); worst=worst or 7
    elif ",unknown-c," in S and job['name']=='c' and n == 1: row.update(job_id=None, outcome="unknown", rc=1, error="outcome unknown: no response line (request "+row["request_id"]+")", fix="rerun with the same --request-id"); worst=worst or 1
    else: row.update(job_id=f"job-{job['name']}", created=(n == 1), outcome=("created" if n == 1 else "existing"), out=job['out'], log="/x/lane.log", rc=0)
    print(json.dumps(row, sort_keys=True))
sys.exit(worst)
PY
    exit $?;;
  wait)
    shift; ids=(); while [ $# -gt 0 ]; do case "$1" in --timeout) shift;; --json) ;; *) ids+=("$1");; esac; shift; done
    case "$S" in *,unknown-id,*) echo "subfleet wait: unknown job ${ids[0]}" >&2; exit 2;; esac
    case "$S" in *,wait-down-first,*) if [ "$n" -eq 1 ]; then echo "subfleet wait: daemon unavailable" >&2; exit 69; fi;; esac
    done_at=2; case "$S" in *,wait-down-first,*) done_at=3;; esac
    if [ "$n" -lt "$done_at" ]; then for id in "${ids[@]}"; do echo "{\"job_id\": \"$id\", \"state\": \"running\", \"timeout\": true, \"waited_s\": 540.0}"; done; exit 124; fi
    worst=0; m=$(ls *.manifest.json | head -1)
    for id in "${ids[@]}"; do name=${id#job-}; out=$(python3 -c "import json,sys; print(next(j['out'] for j in json.load(open('$m'))['jobs'] if j['name']=='$name'))")
      nm="\"name\": \"$name\", "; case "$S" in *,no-name,*) nm="";; esac
      if [ "$name" = c ] && case "$S" in *,lost-c,*) true;; *) false;; esac; then echo "{\"job_id\": \"$id\", ${nm}\"state\": \"lost\", \"rc\": 125, \"out_path\": \"$out\", \"export_error\": null}"; [ $worst -lt 125 ] && worst=125
      elif [ "$name" = a ] && case "$S" in *,failed-a,*) true;; *) false;; esac; then echo "{\"job_id\": \"$id\", ${nm}\"state\": \"failed\", \"rc\": 1, \"out_path\": \"$out\", \"export_error\": null}"; [ $worst -lt 1 ] && worst=1
      else printf 'hello from %s\n' "$name" > "$out"; echo "{\"job_id\": \"$id\", ${nm}\"state\": \"succeeded\", \"rc\": 0, \"out_path\": \"$out\", \"export_error\": null}"; fi
    done; exit $worst;;
  *) echo "fake: unexpected verb $1" >&2; exit 99;;
esac
