#!/bin/bash
# Fake subfleet for the fanout harness. Never touches the daemon. Replays the
# row shapes of `subfleet run --batch --json` and `subfleet wait --json` on
# this PR's base 81d58fe9 (cli.py cmd_run_batch, wait_jobs).
# SCENARIO: comma list of refuse-b, submit-down, unknown-c, wait-down-first,
#   submit-down-always, manifest-refusal, manifest-refusal-marker,
#   rowless-unknown, digest-conflict-c, lost-c, failed-a, unknown-id, no-name,
#   check-prompts, lose-stdout, lookup-down-always, lookup-empty, lookup-bad-json,
#   unknown-a, resend-moved, resend-moved-c, resend-invalid-c
S=",${SCENARIO:-},"; verb="$1"; [ "$1" = runs ] && [ "${2:-}" = show ] && verb=show
if [ "$1" = run ]; then for arg in "$@"; do [ "$arg" = --dry-run ] && verb=preview; done; fi
C="$FAKE_STATE/calls.$verb"; n=$(cat "$C" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$C"
echo "fake subfleet $*" >&2
# FAKE_POLICY_RETIRE=old:new is a daemon policy that retires a current model to
# a new short name outside the CLI's MODEL_CHOICES (both the preview and batch).
RETIRE_PY='
def retire(policy):
    for pair in filter(None, os.environ.get("FAKE_POLICY_RETIRE", "").split(",")):
        old, new = pair.split(":")
        policy["models"][new] = {**policy["models"].pop(old), "id": "fake-" + new}
        policy["retired"][old] = new
    return policy
'
export RETIRE_PY
case "$1" in
  run)
    if [ "$verb" = preview ]; then
      [ "${FAKE_POLICY_PROOF:-}" = hang ] && exec sleep 30
      python3 - "$@" <<'PY'
import json,os,sys
from pathlib import Path
sys.path.insert(0, os.environ["FANOUT_FAKE_REPOSITORY"])
from subfleet.policy import resolve_model, RETIRED_MODELS
exec(os.environ["RETIRE_PY"])
policy=retire(json.loads(Path(os.environ["FANOUT_FAKE_REPOSITORY"], "subfleet/default_policy.json").read_text()))
model=sys.argv[sys.argv.index("-m")+1] if "-m" in sys.argv else None
chain=[resolve_model(policy, RETIRED_MODELS.get(model, model), note=False)] if model else ["opus"]
mode=os.environ.get("FAKE_POLICY_PROOF", "")
if mode == "down": sys.exit(69)
if mode == "bad": print("not JSON"); sys.exit(0)
print(json.dumps({"dry_run":True, "decision":{} if mode == "missing" else {"policy_hash":os.environ.get("FAKE_POLICY_HASH", "policy-one"), "chain":chain, "chosen_model":None}}))
PY
      exit $?;
    fi
    shift; m=""; rid=""; detach=0; while [ $# -gt 0 ]; do case "$1" in --batch) m="$2"; shift;; --request-id) rid="$2"; shift;; -d) detach=1;; esac; shift; done
    [ "$detach" = 1 ] || { echo "fake: -d missing" >&2; exit 98; }
    SCEN="$S" RID="$rid" N="$n" python3 - "$m" <<'PY'
import ast,base64,hashlib,json,re,sys,os
from pathlib import Path
sys.path.insert(0, os.environ["FANOUT_FAKE_REPOSITORY"])
from subfleet.policy import resolve_model, RETIRED_MODELS
exec(os.environ["RETIRE_PY"])
policy=retire(json.loads(Path(os.environ["FANOUT_FAKE_REPOSITORY"], "subfleet/default_policy.json").read_text()))
doc=json.load(open(sys.argv[1])); S=os.environ["SCEN"]; n=int(os.environ["N"]); rid=os.environ["RID"] or f"auto-{n}"; worst=0
# load_batch checks every entry's model against the CLI's own MODEL_CHOICES
# before anything is sent (cli.py BATCH_CHOICES); read from the repository.
cli=Path(os.environ["FANOUT_FAKE_REPOSITORY"], "subfleet/cli.py").read_text()
choices=ast.literal_eval(re.search(r"^MODEL_CHOICES = (\(.*\))$", cli, re.M).group(1))
for i,job in enumerate(doc["jobs"],1):
    if "model" in job and job["model"] not in choices:
        print(f"subfleet: run --batch: jobs[{i}]: model must be one of {', '.join(choices)}", file=sys.stderr)
        sys.exit(2)
if os.environ.get("FAKE_CAPTURE_ATTEMPT"):
    Path(os.environ["FAKE_CAPTURE_ATTEMPT"]).write_text(json.dumps({job["name"]:hashlib.sha256(Path(job["prompt"]).read_bytes()).hexdigest() for job in doc["jobs"]}))
registry_path=Path(os.environ["FAKE_STATE"]) / "registry.json"
registry=json.loads(registry_path.read_text()) if registry_path.exists() else {}
def commit(job, index, head=None, policy_hash=None):
    request_id=f"{rid}-{index}"
    prompt=Path(job["prompt"]).read_bytes()
    payload={"workdir_head":head or os.environ.get("FAKE_SUBMIT_HEAD", os.environ.get("FAKE_WORKDIR_HEAD", "head-one")),
             "policy_hash":policy_hash or os.environ.get("FAKE_SUBMIT_POLICY", os.environ.get("FAKE_POLICY_HASH", "policy-one")),
             "workdir":os.path.realpath(job["workdir"]), "out_path":os.path.realpath(job["out"]),
             "task":job["task"], "tier":job["tier"], "sandbox":job.get("sandbox", "read-only"),
             "pinned_model":resolve_model(policy, RETIRED_MODELS.get(job["model"], job["model"]), note=False) if job.get("model") else None,
             "prompt_sha256":hashlib.sha256(prompt).hexdigest()}
    # C-6.2: display name and label are excluded, payload differences refuse.
    canonical={k:v for k,v in payload.items() if k != "prompt_sha256"}
    canonical.update(prompt=base64.b64encode(prompt).decode("ascii"), pinned_lane=None, exclusions=[], allow_desktop=False)
    digest=hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if os.environ.get("FAKE_CAPTURE_PAYLOADS"):
        with Path(os.environ["FAKE_CAPTURE_PAYLOADS"]).open("a") as evidence:
            evidence.write(json.dumps({**payload, "request_id":request_id, "payload_digest":digest})+chr(10))
    existing=registry.get(request_id)
    if existing:
        return existing, False, existing["payload_digest"] != digest
    stored_prompt=registry_path.parent / "registry-prompts" / request_id / "prompt.md"
    stored_prompt.parent.mkdir(parents=True, exist_ok=True)
    stored_prompt.write_bytes(prompt)
    registry[request_id]={**payload, "job_id":f"job-{job['name']}", "request_id":request_id,
                         "name":job["name"], "state":"queued", "payload_digest":digest,
                         "prompt_path":str(stored_prompt),
                         "batch":{"id":rid, "label":doc["label"], "index":index, "size":len(doc["jobs"])}}
    (stored_prompt.parent / "manifest.json").write_text(json.dumps({"job":{k:v for k,v in registry[request_id].items() if k != "batch"}, "batch":registry[request_id]["batch"]}))
    registry_path.write_text(json.dumps(registry, sort_keys=True))
    return registry[request_id], True, False
missing_prompt=",check-prompts," in S and any(not Path(job["prompt"]).is_file() for job in doc["jobs"])
if missing_prompt:
    i,job=next((i,job) for i,job in enumerate(doc["jobs"],1) if not Path(job["prompt"]).is_file())
    print(f"subfleet: run: prompt {job['prompt']} is not a file", file=sys.stderr)
    print(f"subfleet run --batch: jobs[{i}] ({job['name']}) is invalid; nothing was submitted", file=sys.stderr)
    sys.exit(2)
if ",manifest-refusal," in S or ",manifest-refusal-marker," in S:
    print("subfleet: run: -C /missing is not a directory", file=sys.stderr)
    print("subfleet run --batch: jobs[1] (a) is invalid; nothing was submitted", file=sys.stderr)
    sys.exit(1 if ",manifest-refusal-marker," in S else 2)
if (",rowless-unknown," in S and n == 1) or ",lose-stdout," in S:
    for i,job in enumerate(doc["jobs"],1): commit(job,i)
    print("subfleet: run: outcome unknown: no response line", file=sys.stderr)
    sys.exit(1)
down = (",submit-down," in S and n == 1) or ",submit-down-always," in S
for i,job in enumerate(doc['jobs'],1):
    row={"batch":rid,"label":doc['label'],"index":i,"name":job['name'],"workdir":job['workdir'],"request_id":f"{rid}-{i}"}
    if down: row.update(job_id=None, outcome="not-sent", rc=69, error="daemon unavailable: connection refused", fix="start the daemon and rerun the same manifest with the same --request-id"); worst=worst or 69
    elif ",refuse-b," in S and job['name']=='b': row.update(job_id=None, outcome="refused", rc=7, error="output path is held by another job", fix="use a different -o path"); worst=worst or 7
    elif job['name']=='c' and ((",unknown-c," in S and n == 1) or (",unknown-c-after-old," in S and n == 2)): commit(job,i); row.update(job_id=None, outcome="unknown", rc=1, error="outcome unknown: no response line (request "+row["request_id"]+")", fix="rerun with the same --request-id"); worst=worst or 1
    elif job['name']=='a' and ",unknown-a," in S and n == 1: commit(job,i); row.update(job_id=None, outcome="unknown", rc=1, error="outcome unknown: no response line (request "+row["request_id"]+")", fix="rerun with the same --request-id"); worst=worst or 1
    elif ",resend-moved," in S or (job['name']=='c' and (",resend-moved-c," in S or ",resend-invalid-c," in S)):
        # client.py call_settled: the first send's answer is lost, and the CLI
        # re-sends the same request id. HEAD or policy moved in between
        # (FAKE_RESEND_MOVES=head|policy|none), so the daemon may refuse it as a
        # different payload; _settle_refused_submit then names the job holding
        # the id, which the first send may have just created.
        commit(job,i)
        moves=os.environ.get("FAKE_RESEND_MOVES", "head")
        recorded,_,conflict=commit(job,i, head="moved-at-resend" if moves == "head" else None,
                                   policy_hash="moved-at-resend" if moves == "policy" else None)
        if ",resend-invalid-c," in S:
            row.update(job_id=None, outcome="refused", rc=2,
                       error=f"max_attempts must be positive and within policy caps; job {recorded['job_id']} carries request id {row['request_id']}")
            worst=worst or 2
        elif conflict:
            row.update(job_id=None, outcome="refused", rc=2,
                       error="request id already used with a different payload by job " + recorded["job_id"])
            worst=worst or 2
        else:
            row.update(job_id=recorded["job_id"], created=False, outcome="existing", requeried=True,
                       out=recorded["out_path"], log="/x/lane.log", rc=0)
    elif ",digest-conflict-c," in S and job['name']=='c': row.update(job_id=None, outcome="refused", rc=2, error="request id already used with a different payload by job job-c"); worst=worst or 2
    else:
        recorded,created,conflict=commit(job,i)
        if conflict:
            row.update(job_id=None, outcome="refused", rc=2,
                       error="request id already used with a different payload by job " + recorded["job_id"])
            worst=worst or 2
        else:
            row.update(job_id=recorded["job_id"], created=created, outcome="created" if created else "existing",
                       out=recorded["out_path"], log="/x/lane.log", rc=0)
    print(json.dumps(row, sort_keys=True))
sys.exit(worst)
PY
    exit $?;;
  runs)
    if [ "${2:-}" = show ]; then
      [ -z "${CLAUDE_CODE_SESSION_ID:-}" ] || { echo "fake: show would acknowledge caller notices" >&2; exit 98; }
      python3 - "$FAKE_STATE/registry.json" "$3" <<'PY'
import json,os,sys
registry=json.load(open(sys.argv[1]))
job=next((row.copy() for row in registry.values() if row["job_id"] == sys.argv[2]), None)
if job is None: sys.exit(2)
if os.environ.get("FAKE_SHOW_MISSING"): job.pop(os.environ["FAKE_SHOW_MISSING"], None)
if os.environ.get("FAKE_SHOW_MISMATCH"): job[os.environ["FAKE_SHOW_MISMATCH"]]="different"
batch=job.pop("batch", None)
if os.environ.get("FAKE_SHOW_FLAT"):
    job["offline"]=True
    print(json.dumps(job, sort_keys=True))
else:
    print(json.dumps({"job":job, "batch":batch, "notices":[]}, sort_keys=True))
PY
      exit $?;
    fi
    shift; rid=""; while [ $# -gt 0 ]; do case "$1" in --request-id) rid="$2"; shift;; --json) ;; *) echo "fake: unexpected runs argument $1" >&2; exit 99;; esac; shift; done
    case "$S" in *,lookup-down-always,*) echo "subfleet runs: daemon unavailable" >&2; exit 69;; esac
    case "$S" in *,lookup-empty,*) exit 0;; esac
    case "$S" in *,lookup-bad-json,*) echo "not JSON"; exit 0;; esac
    python3 - "$FAKE_STATE/registry.json" "$rid" <<'PY'
import json,sys
from pathlib import Path
path=Path(sys.argv[1]); registry=json.loads(path.read_text()) if path.exists() else {}
if sys.argv[2] in registry: print(json.dumps(registry[sys.argv[2]], sort_keys=True))
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
