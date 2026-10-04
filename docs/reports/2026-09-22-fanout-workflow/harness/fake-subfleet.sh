#!/bin/bash
# Fake subfleet for the fanout harness. Never touches the daemon.
# SCENARIO: comma list of refuse-b, submit-down, wait-down-first, lost-c, unknown-id, no-name
S=",${SCENARIO:-},"; C="$FAKE_STATE/calls.$1"; n=$(cat "$C" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$C"
echo "fake subfleet $*" >&2
case "$1" in
  run)
    shift; m=""; rid=""; while [ $# -gt 0 ]; do case "$1" in --batch) m="$2"; shift;; --request-id) rid="$2"; shift;; esac; shift; done
    case "$S" in *,submit-down,*) echo "subfleet run: daemon unavailable" >&2; exit 69;; esac
    SCEN="$S" RID="$rid" python3 - "$m" <<'PY'
import json,sys,os
doc=json.load(open(sys.argv[1])); S=os.environ["SCEN"]; rid=os.environ["RID"] or "auto-uuid"; worst=0
for i,job in enumerate(doc['jobs'],1):
    row={"batch":rid,"label":doc['label'],"index":i,"name":job['name'],"workdir":job['workdir'],"request_id":f"{rid}-{i}"}
    if ",refuse-b," in S and job['name']=='b': row.update(job_id=None, rc=7, error="output path is held by another job", fix="use a different -o path"); worst=worst or 7
    else: row.update(job_id=f"job-{job['name']}", created=True, out=job['out'], log="/x/lane.log", rc=0)
    print(json.dumps(row))
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
      if [ "$name" = c ] && case "$S" in *,lost-c,*) true;; *) false;; esac; then echo "{\"job_id\": \"$id\", ${nm}\"state\": \"lost\", \"rc\": 125, \"out_path\": \"$out\", \"export_error\": null}"; worst=125
      else printf 'hello from %s\n' "$name" > "$out"; echo "{\"job_id\": \"$id\", ${nm}\"state\": \"succeeded\", \"rc\": 0, \"out_path\": \"$out\", \"export_error\": null}"; fi
    done; exit $worst;;
  *) echo "fake: unexpected verb $1" >&2; exit 99;;
esac
