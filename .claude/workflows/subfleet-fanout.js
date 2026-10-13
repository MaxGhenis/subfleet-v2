export const meta = {
  name: 'subfleet-fanout',
  description: 'Fan jobs out through subfleet: one Haiku dispatcher submits a batch manifest, then loops subfleet wait in 540 s slices until no job is pending and reports every job',
  whenToUse: 'Script control flow over heavy workers. The workers run on subfleet lanes (another account or model); only one thin dispatcher spends this login. Check `subfleet status` first: a queued batch costs about 90,000 login tokens per 540 s wait slice for a small batch, more as the manifest grows. args: [{task, tier, dir, promptPath, outPath, name, model?, sandbox?}], or {items, label, requestId, stateDir, maxWaitLoops, allowTmp, model?, sandbox?} (top-level model/sandbox are defaults for every item). Pass requestId to make a relaunch idempotent and resumable. Call it from another script with workflow("subfleet-fanout", args).',
  phases: [
    { title: 'Dispatch', detail: 'one agent: write a checksummed helper and manifest, subfleet run --batch (retry only for not-sent, unknown or missing answers under a request id, at most 3 calls), then subfleet wait --timeout 540 until PENDING_COUNT is 0', model: 'haiku' },
  ],
}

// Why the dispatcher looks like this (subfleet 2.0.0a0 at this PR's base 81d58fe9,
// read 2026-10-04; the measured run and five review rounds are in
// docs/reports/2026-09-22-fanout-workflow.md):
// - `run --batch FILE --json -d` submits every manifest entry in one call and
//   prints one JSON object per entry: job_id with outcome `created` or
//   `existing`, or job_id null with outcome `refused`, `not-sent` (the daemon
//   was not reached) or `unknown` (the answer was lost; the job may exist)
//   (C-17.7, C-16.3; subfleet/cli.py cmd_run_batch). `-d` keeps the call from
//   blocking when SUBFLEET_RUN_DETACH=0 is inherited.
// - `--request-id ID` names entry n `ID-n`; resubmitting the same manifest
//   returns the existing jobs with created:false (C-6.2; Daemon.submit). The
//   index is part of the id, so with a request id the FULL manifest is always
//   submitted and the daemon dedupes; dropping delivered items would shift
//   every later entry onto another entry's id. A not-sent, unknown or missing
//   answer triggers RETRY_STEP1 under a request id (at most 3 calls per run);
//   a refused entry never triggers retries. A later unanswered invocation
//   makes even a previously refused entry unknown. Unknowns survive later
//   not-sent or refused attempts. A row-less validation refusal stops new
//   submissions; earlier unknown requests are looked up with `runs
//   --request-id ID --json`, and remain unsettled if lookup is inconclusive.
//   Recovery (including C-6.2 digest conflicts) verifies recorded job identity
//   against a snapshot from an unanswered invocation before adopting an id.
//   This still works after prompt deletion or workdir head/policy changes.
//   Without a request id, items
//   whose outPath is already non-empty are dropped before the first submission
//   as a heuristic (the daemon never clears a stale -o file), and a relaunch
//   refuses to submit again and prints the command that resets its state.
// - State is bound to the manifest: files are keyed by the request id, or by
//   the label plus a digest of the manifest; a key whose stored manifest
//   differs stops with STATE_MISMATCH. Submit logs are numbered and never
//   truncated; job ids are written only after a log is parsed.
// - Step 0 checksums what the agent retyped: the helper and the manifest are
//   compared with CRC-32 values computed here before anything runs.
// - The Bash tool caps one call at 600 s, so the wait is sliced:
//   `wait <ids> --timeout 540 --json` prints a JSON object per finished job
//   and {"job_id", "state": "running", "timeout": true} per pending one, and
//   exits 124 while any job is pending (C-15.4, C-17.3; cli.py wait_jobs).
//   The daemon answers a multi-job wait only when every job is terminal and
//   exported (Daemon.wait), so the loop is driven by the count of pending
//   rows; the exit code is the maximum over jobs and says nothing about
//   which. Exit 1 or 69 with no parseable row means the daemon did not
//   answer; the helper sleeps for what is left of the slice, at most 60 s,
//   and the loop continues. Exit 1 with rows is a job that failed with rc 1.
// - Each 540 s slice outlives the prompt cache's 5-minute lifetime, so every
//   slice re-writes the dispatcher's whole context, twice (about 45,000
//   tokens each for a small batch, observed 2026-09-22). Queue time counts:
//   with no open lane a batch waited 3.7 h before its first job started.
// - Recovery alone uses `runs show --json`, with CLAUDE_CODE_SESSION_ID removed
//   from the child environment so cli.py _ack_notices returns without
//   acknowledging the launching session's notices (C-15.3).

const TASKS = ['lookup', 'research', 'sweep', 'review', 'build', 'authored-prose', 'strategy', 'adjudication']
const TIERS = ['trivial', 'easy', 'standard', 'hard']
const WAIT_S = 540
const BASH_TIMEOUT_MS = 600000
const LABEL_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

const opts = Array.isArray(args) ? { items: args } : (args || {})
const items = opts.items
if (!Array.isArray(items) || items.length === 0) {
  throw new Error('subfleet-fanout: args must be a non-empty list of {task, tier, dir, promptPath, outPath, name}')
}
if (items.length > 256) throw new Error('subfleet-fanout: a batch manifest holds at most 256 jobs (C-17.7)')
const positiveInt = (value, fallback) => (Number.isInteger(value) && value > 0 ? value : fallback)
// 20 slices of 540 s is 3 h of queue plus run time, about 1.8M login tokens if all are spent waiting.
const MAX_WAIT_LOOPS = positiveInt(opts.maxWaitLoops, 20)
const label = opts.label === undefined ? 'fanout' : opts.label
const requestId = opts.requestId === undefined ? null : opts.requestId
const allowTmp = opts.allowTmp === true

const problems = []
const isPath = value => typeof value === 'string' && value.startsWith('/') && !/[\n\r]/.test(value)
const isWord = value => typeof value === 'string' && LABEL_RE.test(value)
if (!isWord(label)) problems.push(`label must match ${LABEL_RE}`)
if (requestId !== null && !isWord(requestId)) problems.push(`requestId must match ${LABEL_RE}`)
if (opts.stateDir !== undefined && !isPath(opts.stateDir)) problems.push('stateDir must be an absolute path')
if (opts.model !== undefined && !isWord(opts.model)) problems.push('model must be a short model name as `subfleet run -m` takes it')
if (opts.sandbox !== undefined && !['read-only', 'workspace-write'].includes(opts.sandbox)) problems.push('sandbox must be read-only or workspace-write')
const seenOut = new Set(), seenName = new Set()
items.forEach((item, i) => {
  const at = `item ${i}${item && item.name ? ` (${item.name})` : ''}`
  if (!item || typeof item !== 'object') return problems.push(`${at}: not an object`)
  if (!TASKS.includes(item.task)) problems.push(`${at}: task must be one of ${TASKS.join(', ')}`)
  if (!TIERS.includes(item.tier)) problems.push(`${at}: tier must be one of ${TIERS.join(', ')}`)
  for (const key of ['dir', 'promptPath', 'outPath']) {
    if (!isPath(item[key])) problems.push(`${at}: ${key} must be an absolute path`)
  }
  if (isPath(item.dir) && !allowTmp && /^\/(private\/)?tmp(\/|$)/.test(item.dir)) {
    problems.push(`${at}: dir under /tmp is refused by subfleet (C-2.4); pass allowTmp: true to add --allow-tmp`)
  }
  // Model names are not validated here: `run --batch` refuses an unknown one
  // with exit 2 naming the entry, and the list changes with policy.
  if (item.model !== undefined && !isWord(item.model)) problems.push(`${at}: model must be a short model name`)
  if (item.sandbox !== undefined && !['read-only', 'workspace-write'].includes(item.sandbox)) problems.push(`${at}: sandbox must be read-only or workspace-write`)
  if (!isWord(item.name)) problems.push(`${at}: name must match ${LABEL_RE}`)
  if (seenName.has(item.name)) problems.push(`${at}: name ${item.name} is used twice`)
  if (seenOut.has(item.outPath)) problems.push(`${at}: outPath ${item.outPath} is used twice; two jobs would write one file`)
  seenName.add(item.name); seenOut.add(item.outPath)
})
if (problems.length) throw new Error(`subfleet-fanout: ${problems.join('; ')}`)

const manifest = {
  label,
  jobs: items.map(item => {
    const job = { task: item.task, tier: item.tier, workdir: item.dir, prompt: item.promptPath, out: item.outPath, name: item.name }
    const model = item.model === undefined ? opts.model : item.model
    const sandbox = item.sandbox === undefined ? opts.sandbox : item.sandbox
    if (model !== undefined) job.model = model
    if (sandbox !== undefined) job.sandbox = sandbox
    return job
  }),
}
// One job per line, single-encoded: what the agent retypes is plain JSON.
const manifestText = `{"label": ${JSON.stringify(label)}, "jobs": [\n${manifest.jobs.map(job => JSON.stringify(job)).join(',\n')}\n]}\n`

// CRC-32 over the code points (4 bytes each, big-endian) of a string, which
// Python reproduces with zlib.crc32(text.encode("utf-32-be")).
const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1
  return c >>> 0
})
function crc32(text) {
  let crc = 0xFFFFFFFF
  for (const ch of text) {
    const cp = ch.codePointAt(0)
    for (const byte of [(cp >>> 24) & 255, (cp >>> 16) & 255, (cp >>> 8) & 255, cp & 255]) {
      crc = CRC_TABLE[(crc ^ byte) & 255] ^ (crc >>> 8)
    }
  }
  return (crc ^ 0xFFFFFFFF) >>> 0
}
function fnv1a(text) {
  let hash = 0x811C9DC5
  for (const ch of text) {
    hash ^= ch.codePointAt(0)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash.toString(16).padStart(8, '0')
}

// Every state file carries the request id when there is one, else the label
// and a digest of the manifest, so two batches never read each other's ids.
const key = requestId || `${label}-${fnv1a(manifestText)}`
const stateDir = opts.stateDir || `${items[0].outPath.slice(0, items[0].outPath.lastIndexOf('/'))}/.subfleet-fanout`
const q = value => `'${String(value).replace(/'/g, `'\\''`)}'`
const files = {
  helper: `${key}.fanout.py`, all: `${key}.manifest.all.json`, incoming: `${key}.manifest.new.json`,
  manifest: `${key}.manifest.json`, submitPrefix: `${key}.submit.`, submitErr: `${key}.submit.err`,
  jobs: `${key}.jobs.txt`, wait: `${key}.wait.jsonl`, waitErr: `${key}.wait.err`,
  refusal: `${key}.submit.refusal.json`, recovered: `${key}.recovered.jsonl`,
}
const HD = 'FANOUT_PY_EOF'
const HM = 'FANOUT_MANIFEST_EOF'
const helper = `import glob, hashlib, json, os, re, shlex, subprocess, sys, time, zlib
KEY = ${JSON.stringify(key)}
REQUEST_ID = ${JSON.stringify(requestId || '')}
F = ${JSON.stringify(files)}
NL = chr(10)
RETRY_S = int(os.environ.get("SUBFLEET_FANOUT_RETRY_S", "60"))
SLICE_BUDGET_S = 590

def out(line):
    print(line, flush=True)

def crc(path):
    with open(path, encoding="utf-8") as fh:
        return zlib.crc32(fh.read().encode("utf-32-be")) & 0xFFFFFFFF

def size(path):
    try:
        return os.path.getsize(path) if os.path.isfile(path) else 0
    except OSError:
        return 0

def lines(path):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]

def rows(path, kind):
    found = []
    for line in lines(path):
        try:
            found.append(json.loads(line))
        except ValueError:
            out("BAD_" + kind + "_LINE " + line[:160])
    return found

def tail(path, n=3):
    return " | ".join(lines(path)[-n:])

def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)

def submit_logs():
    found = glob.glob(F["submitPrefix"] + "*.jsonl")
    return sorted(found, key=lambda p: int(p[len(F["submitPrefix"]):-6]))

IDENTITY_FIELDS = ("name", "workdir", "out_path", "task", "tier", "sandbox", "pinned_model")

def prompt_digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()

def entry_identity(job):
    return {"name": job["name"], "workdir": os.path.realpath(job["workdir"]),
            "out_path": os.path.realpath(job["out"]), "task": job["task"], "tier": job["tier"],
            "sandbox": job.get("sandbox", "read-only"), "pinned_model": job.get("model"),
            "prompt_sha256": prompt_digest(job["prompt"])}

def recorded_identity(row, digest):
    # Missing fields are not evidence, including a missing null model pin.
    if any(field not in row for field in IDENTITY_FIELDS):
        return None
    return {**{field: row[field] for field in IDENTITY_FIELDS}, "prompt_sha256": digest}

def recovery_matches(row, s):
    identity = row.get("identity")
    return (isinstance(identity, dict) and identity in s["identities"]
            and recorded_identity(row, identity.get("prompt_sha256")) == identity
            and row.get("request_id") == s["request_id"]
            and isinstance(row.get("job_id"), str) and bool(row["job_id"]))

def settled(manifest_names):
    """Keep a job id or unresolved uncertainty across all submission attempts."""
    state = {name: {"job_id": "", "outcome": "", "row": {}, "identities": [],
                    "request_id": (REQUEST_ID + "-" + str(i)) if REQUEST_ID else ""}
             for i, name in enumerate(manifest_names, 1)}
    logs = submit_logs()
    refusal = load(F["refusal"]) if os.path.isfile(F["refusal"]) else None
    # Older helpers omitted the log name; their refusal belongs to the last log.
    refused_log = refusal.get("log", logs[-1] if logs else "") if refusal else ""
    for path in logs:
        if path == refused_log:
            continue  # This invocation sent nothing; it cannot erase earlier unknowns.
        try:
            identities = load(path + ".identity.json")
        except (OSError, ValueError):
            identities = {}  # Legacy state cannot prove an entry's original prompt.
        if not isinstance(identities, dict):
            identities = {}
        answered = set()
        for row in rows(path, "SUBMIT"):
            name = row.get("name")
            if name not in state:
                continue
            answered.add(name)
            s = state[name]
            if s["job_id"]:
                continue  # A known job remains known across full-manifest retries.
            outcome = row.get("outcome") or ("created" if row.get("job_id") else "refused")
            jid = row.get("job_id")
            conflict = re.search(r"request id already used with a different payload by job (\\S+)", row.get("error") or "")
            if not jid and REQUEST_ID and s["outcome"] == "unknown" and conflict:
                s["candidate_job_id"] = conflict.group(1)  # Verify before adoption.
            if jid and not s["job_id"]:
                s["job_id"] = jid
                s["created"] = row.get("created")
            if not jid and s["outcome"] == "unknown":
                if outcome == "unknown" and identities.get(name):
                    s["identities"].append(identities[name])
                continue  # A later refusal or unsent attempt cannot settle the first.
            s["request_id"] = row.get("request_id") or s["request_id"]
            s["outcome"] = outcome
            s["row"] = row
            if outcome == "unknown" and identities.get(name):
                s["identities"].append(identities[name])
        for name, s in state.items():
            if name not in answered and not s["job_id"]:
                s["outcome"] = "unknown"
                s["row"] = {"rc": -1, "error": "no row", "request_id": s["request_id"]}
                if identities.get(name):
                    s["identities"].append(identities[name])
    if refusal:
        for s in state.values():
            if not s["job_id"] and s["outcome"] != "unknown":
                s["outcome"], s["row"] = "refused", refusal
    for row in rows(F["recovered"], "RECOVERY"):
        s = state.get(row.get("name"))
        if s and not s["job_id"] and recovery_matches(row, s):
            s.update(job_id=row["job_id"], created=False, outcome="existing", row=row)
    return state

def recover_unknown(state):
    """Verify ledger/conflict candidates against unanswered invocation snapshots."""
    deadline = time.monotonic() + 60
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    for name, s in state.items():
        if s["job_id"] or s["outcome"] != "unknown" or not s["request_id"]:
            continue
        jid = s.get("candidate_job_id")
        if not jid and not os.path.isfile(F["refusal"]):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        rid = s["request_id"]
        try:
            if not jid:
                result = subprocess.run(["subfleet", "runs", "--request-id", rid, "--json"],
                                        capture_output=True, text=True, timeout=min(15, remaining), env=env)
                if result.returncode != 0:
                    raise ValueError("request lookup rc=%s: %s" % (result.returncode, result.stderr.strip()))
                found = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                matches = [row for row in found if isinstance(row, dict) and row.get("request_id") == rid
                           and isinstance(row.get("job_id"), str) and row["job_id"]]
                if len(matches) != 1:
                    # An empty offline store cannot prove the request never ran.
                    raise ValueError("no unique matching job; earlier outcome remains unknown")
                jid = matches[0]["job_id"]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = subprocess.run(["subfleet", "runs", "show", jid, "--json"],
                                    capture_output=True, text=True, timeout=min(15, remaining), env=env)
            if result.returncode != 0:
                raise ValueError("show rc=%s: %s" % (result.returncode, result.stderr.strip()))
            row = json.loads(result.stdout)
            # Online show wraps metadata in job; the offline CLI emits it flat.
            if isinstance(row, dict) and isinstance(row.get("job"), dict):
                row = row["job"]
            if not isinstance(row, dict) or row.get("job_id") != jid or row.get("request_id") != rid:
                raise ValueError("show identity missing or mismatched")
            identity = recorded_identity(row, prompt_digest(row["prompt_path"]))
            row = {**row, "identity": identity, "created": False, "outcome": "existing"}
            if not recovery_matches(row, s):
                raise ValueError("job does not match an unanswered manifest entry snapshot")
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
            out("LOOKUP_UNKNOWN name=%s request_id=%s error=%s" % (name, rid, str(exc).split(NL)[0]))
            continue
        with open(F["recovered"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + NL)
        s.update(job_id=row["job_id"], created=False, outcome="existing", row=row)

def retry_names(state):
    return [name for name, s in state.items() if not s["job_id"] and s["outcome"] in ("", "not-sent", "unknown")]

def write_jobs(ids):
    with open(F["jobs"], "w", encoding="utf-8") as fh:
        fh.write(NL.join(ids) + NL if ids else "")

def cmd_check(expected_helper, expected_manifest):
    ok = True
    for path, expected in ((os.path.abspath(__file__), expected_helper), (F["incoming"], expected_manifest)):
        got = crc(path)
        if got != int(expected):
            ok = False
            out("CHECKSUM_MISMATCH file=%s expected=%s got=%s" % (os.path.basename(path), expected, got))
    if not ok:
        sys.exit(1)
    out("CHECKSUM_OK")

def cmd_setup():
    incoming = load(F["incoming"])
    if os.path.isfile(F["all"]):
        if load(F["all"]) != incoming:
            out("STATE_MISMATCH key=%s: this state directory holds a different manifest under the same key" % KEY)
            sys.exit(1)
    os.replace(F["incoming"], F["all"])
    if submit_logs():
        names = [job["name"] for job in load(F["manifest"])["jobs"]] if os.path.isfile(F["manifest"]) else []
        ids = [s["job_id"] for s in settled(names).values() if s["job_id"]]
        write_jobs(ids)
        out("ALREADY_SUBMITTED jobs=%s" % len(ids))
        out("SETUP_OK")
        return
    keep = []
    for job in incoming["jobs"]:
        n = size(job["out"])
        if n > 0 and not REQUEST_ID:
            out("SKIP name=%s out_bytes=%s" % (job["name"], n))
            continue
        if n > 0:
            out("NOTE name=%s out exists (%s bytes); submitted anyway, the request id dedupes" % (job["name"], n))
        keep.append(job)
    with open(F["manifest"], "w", encoding="utf-8") as fh:
        json.dump({"label": incoming["label"], "jobs": keep}, fh, indent=1)
    out("TO_SUBMIT=%s" % len(keep))
    out("SETUP_OK")

def cmd_should_submit():
    if not os.path.isfile(F["manifest"]):
        out("SETUP_MISSING")
        sys.exit(1)
    names = [job["name"] for job in load(F["manifest"])["jobs"]]
    if not names:
        out("NOTHING_TO_SUBMIT")
        sys.exit(1)
    logs = submit_logs()
    if not logs:
        sys.exit(0)
    state = settled(names)
    missing = retry_names(state)
    if os.path.isfile(F["refusal"]):
        out("ALREADY_SUBMITTED")
        sys.exit(1)  # post-submit still looks up any earlier unanswered requests.
    if REQUEST_ID and missing:
        out("RESUBMIT names=%s" % ",".join(missing))
        latest = [r for r in rows(logs[-1], "SUBMIT") if (r.get("outcome") or "") in ("not-sent", "unknown")]
        if latest:
            time.sleep(min(RETRY_S, 60))
            out("RETRY_SLEPT=%s" % min(RETRY_S, 60))
        sys.exit(0)
    out("ALREADY_SUBMITTED")
    if missing:
        out("RESET rm -f %s/%s.*   # %s entr%s never got a job id and there is no request id to settle them" % (shlex.quote(os.getcwd()), KEY, len(missing), "y" if len(missing) == 1 else "ies"))
    sys.exit(1)

def cmd_next_log():
    path = F["submitPrefix"] + str(len(submit_logs()) + 1) + ".jsonl"
    identities = {}
    for job in load(F["manifest"])["jobs"]:
        try:
            identities[job["name"]] = entry_identity(job)
        except OSError:
            pass  # A missing prompt cannot supply proof for recovery.
    with open(path + ".identity.json", "w", encoding="utf-8") as fh:
        json.dump(identities, fh)
    print(path)

def cmd_post_submit(rc):
    everything = load(F["all"])["jobs"]
    names = [job["name"] for job in load(F["manifest"])["jobs"]] if os.path.isfile(F["manifest"]) else []
    logs = submit_logs()
    error = tail(F["submitErr"])
    if rc >= 0 and logs and not rows(logs[-1], "SUBMIT"):
        if error:
            out("SUBMIT_ERR " + error)
        if rc == 2 or "nothing was submitted" in error:
            with open(F["refusal"], "w", encoding="utf-8") as fh:
                json.dump({"rc": 2, "error": error or "run --batch refused the manifest (exit 2)", "log": logs[-1]}, fh)
    state = settled(names)
    if os.path.isfile(F["refusal"]):
        refusal = load(F["refusal"])
        out("MANIFEST_REFUSED rc=%s error=%s" % (refusal["rc"], refusal["error"]))
    recover_unknown(state)
    ids, unsettled, batch = [], 0, ""
    for job in everything:
        name = job["name"]
        if name not in state:
            out("SKIP name=%s out_bytes=%s" % (name, size(job["out"])))
            continue
        s = state[name]
        batch = s["row"].get("batch") or batch
        if s["job_id"]:
            ids.append(s["job_id"])
            out("SUBMITTED name=%s job_id=%s created=%s" % (name, s["job_id"], s.get("created")))
        elif s["outcome"] == "refused":
            out("REFUSED name=%s rc=%s error=%s" % (name, s["row"].get("rc"), s["row"].get("error")))
        elif s["outcome"] in ("not-sent", "unknown"):
            unsettled += 1
            marker = "NOT_SENT" if s["outcome"] == "not-sent" else "OUTCOME_UNKNOWN"
            out("%s name=%s request_id=%s rc=%s error=%s" % (marker, name, s["row"].get("request_id"), s["row"].get("rc"), s["row"].get("error")))
        else:
            unsettled += 1
            out("OUTCOME_UNKNOWN name=%s request_id=%s rc=%s error=no row" % (name, (REQUEST_ID + "-?") if REQUEST_ID else "", -1))
    write_jobs(ids)
    out("REQUEST_ID=%s" % (REQUEST_ID or (batch if ids else "")))
    out("JOBS=%s" % len(ids))
    out("UNSETTLED=%s" % unsettled)
    if REQUEST_ID and retry_names(state) and not os.path.isfile(F["refusal"]):
        out("RETRY_STEP1")

def cmd_post_wait(rc, started):
    out("WAIT_RC=%s" % rc)
    manifest_names = [job["name"] for job in load(F["manifest"])["jobs"]]
    names = {s["job_id"]: name for name, s in settled(manifest_names).items() if s["job_id"]}
    outs = {job["name"]: job["out"] for job in load(F["all"])["jobs"]}
    ids = lines(F["jobs"])
    seen, pending = set(), 0
    found = rows(F["wait"], "WAIT")
    for o in found:
        jid = o.get("job_id")
        name = o.get("name") or names.get(jid) or ""
        seen.add(jid)
        if o.get("timeout"):
            pending += 1
            out("PENDING name=%s job_id=%s waited_s=%s" % (name, jid, o.get("waited_s")))
        else:
            target = o.get("out_path") or outs.get(name) or ""
            out("DONE name=%s job_id=%s state=%s rc=%s out_bytes=%s export_error=%s"
                % (name, jid, o.get("state"), o.get("rc"), size(target), o.get("export_error") or ""))
    missing = [jid for jid in ids if jid not in seen]
    if rc in (1, 69) and not found:
        out("DAEMON_UNREACHABLE rc=%s err=%s" % (rc, tail(F["waitErr"])))
        for jid in missing:
            pending += 1
            out("PENDING name=%s job_id=%s waited_s=0" % (names.get(jid, ""), jid))
        elapsed = time.time() - started if started > 0 else 0
        nap = max(0, min(RETRY_S, int(SLICE_BUDGET_S - elapsed)))
        time.sleep(nap)
        out("RETRY_SLEPT=%s" % nap)
    else:
        for jid in missing:
            out("UNKNOWN name=%s job_id=%s rc=%s error=%s" % (names.get(jid, ""), jid, rc, tail(F["waitErr"])))
    out("PENDING_COUNT=%s" % pending)

def num(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

verb = sys.argv[1] if len(sys.argv) > 1 else ""
if verb == "check":
    cmd_check(sys.argv[2], sys.argv[3])
elif verb == "setup":
    cmd_setup()
elif verb == "should-submit":
    cmd_should_submit()
elif verb == "next-log":
    cmd_next_log()
elif verb == "post-submit":
    cmd_post_submit(num(sys.argv[2] if len(sys.argv) > 2 else None, -1))
elif verb == "post-wait":
    cmd_post_wait(num(sys.argv[2] if len(sys.argv) > 2 else None, -1), num(sys.argv[3] if len(sys.argv) > 3 else None, 0))
else:
    out("BAD_VERB " + verb)
    sys.exit(2)
`
for (const [text, marker] of [[helper, HD], [manifestText, HM], [helper, HM], [manifestText, HD]]) {
  if (text.split('\n').includes(marker)) throw new Error('subfleet-fanout: text collides with a heredoc delimiter')
}

const H = q(files.helper)
const runFlags = `--json -d${allowTmp ? ' --allow-tmp' : ''}${requestId ? ` --request-id ${q(requestId)}` : ''}`
const step0 = `mkdir -p ${q(stateDir)} && cd ${q(stateDir)} && cat > ${H} <<'${HD}' && cat > ${q(files.incoming)} <<'${HM}' && python3 -u ${H} check ${crc32(helper)} ${crc32(manifestText)} && python3 -u ${H} setup\n${helper}${HD}\n${manifestText}${HM}`
const step1 = `cd ${q(stateDir)} && { SUBMIT_RESULT=-1; if python3 -u ${H} should-submit; then subfleet run --batch ${q(files.manifest)} ${runFlags} > "$(python3 ${H} next-log)" 2> ${q(files.submitErr)}; SUBMIT_RESULT=$?; echo "SUBMIT_RC=$SUBMIT_RESULT"; fi; python3 -u ${H} post-submit "$SUBMIT_RESULT"; }`
const step2 = `cd ${q(stateDir)} && if [ -s ${q(files.jobs)} ]; then T0=$(date +%s); subfleet wait $(cat ${q(files.jobs)}) --timeout ${WAIT_S} --json > ${q(files.wait)} 2> ${q(files.waitErr)}; python3 -u ${H} post-wait $? $T0; else echo "NOTHING_TO_WAIT"; echo "PENDING_COUNT=0"; fi`

const STATES = ['skipped', 'refused', 'not-sent', 'submit-unknown', 'succeeded', 'failed', 'cancelled', 'lost', 'running', 'unknown']
const RESULT_SCHEMA = {
  type: 'object',
  properties: {
    setup_ok: { type: 'boolean', description: 'true when step 0 printed CHECKSUM_OK and SETUP_OK' },
    already_submitted: { type: 'boolean', description: 'true when step 0 or step 1 printed ALREADY_SUBMITTED' },
    submit_calls: { type: 'integer', description: 'how many step 1 calls printed a SUBMIT_RC line' },
    submit_rc: { type: 'integer', description: 'the last SUBMIT_RC number; 0 when no submission ran' },
    request_id: { type: 'string', description: 'the REQUEST_ID value from step 1; empty when nothing was submitted' },
    wait_loops: { type: 'integer', description: 'how many step 2 calls were made' },
    last_wait_rc: { type: 'integer', description: 'the WAIT_RC number of the last step 2 call; 0 when step 2 never ran' },
    jobs: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          job_id: { type: 'string', description: 'empty when skipped, refused, not-sent, submit-unknown or unknown' },
          state: { type: 'string', enum: STATES },
          rc: { type: 'integer', description: 'the DONE line rc; 0 when skipped; the REFUSED, NOT_SENT or OUTCOME_UNKNOWN rc; 124 when still pending at the loop cap; -1 when unknown' },
          out_bytes: { type: 'integer' },
          notes: { type: 'string', description: 'the error text of a REFUSED, NOT_SENT, OUTCOME_UNKNOWN or UNKNOWN line, the DONE export_error, or empty' },
        },
        required: ['name', 'job_id', 'state', 'rc', 'out_bytes', 'notes'],
      },
    },
    notes: { type: 'string', description: 'anything unexpected: CHECKSUM_MISMATCH, STATE_MISMATCH, MANIFEST_REFUSED, LOOKUP_UNKNOWN, SUBMIT_ERR, DAEMON_UNREACHABLE, RESET or BAD_*_LINE lines, a step whose marker lines were missing, a killed call; else empty' },
  },
  required: ['setup_ok', 'already_submitted', 'submit_calls', 'submit_rc', 'request_id', 'wait_loops', 'last_wait_rc', 'jobs', 'notes'],
}

const names = items.map(item => item.name)
const prompt = `You are one dispatcher inside a workflow. The main session is already carrying out the user's request shown above, as a whole. It launched this workflow as one step of that work and gave you one mechanical job, so this job does not conflict with the user's request: it is the part of it assigned to you. Do not start the user's request yourself. Do not read its files, load skills, write or edit anything, or commit; the main session does all of that, and a second copy of the work would collide with it.

Run only the Bash commands below, exactly as written, character for character, with the Bash tool's timeout parameter set to ${BASH_TIMEOUT_MS} on every call. Do not run any other subfleet subcommand (no status, runs, kill, pick, why, sessions, gate, codex, claude). Each command prints marker lines in CAPITALS; copy their numbers exactly.

STEP 0. One Bash call. It writes a helper and the manifest, checks that both were typed exactly, and prints CHECKSUM_OK then SETUP_OK:
${step0}

If step 0 does not print SETUP_OK, stop: report setup_ok false, every job as state "unknown" with rc -1, and its output or missing marker in notes. CHECKSUM_MISMATCH and STATE_MISMATCH also mean stop.

STEP 1. One Bash call. It submits the full manifest when should-submit allows it and prints SUBMIT_RC when a submission ran, then SUBMITTED, REFUSED, NOT_SENT, OUTCOME_UNKNOWN or SKIP lines, REQUEST_ID, JOBS and UNSETTLED:
${step1}

Run step 1 again while it prints RETRY_STEP1, at most 3 step 1 calls in total. This marker requires a request id and a not-sent, unknown or missing answer; a refused entry never triggers it. MANIFEST_REFUSED stops new submissions; record its CLI message and any LOOKUP_UNKNOWN lines in notes. Step 1 itself looks up earlier unknown request ids and prints their recovered SUBMITTED lines, or keeps them OUTCOME_UNKNOWN if the lookup is inconclusive. Use those per-name markers even after MANIFEST_REFUSED, then wait on every known id. After the last step 1 call, if JOBS=0, skip step 2.

STEP 2. One Bash call per loop. It waits up to 540 s and prints DONE, PENDING or UNKNOWN lines and PENDING_COUNT:
${step2}

Repeat step 2 while PENDING_COUNT is greater than 0, and stop after ${MAX_WAIT_LOOPS} calls in total. A call that prints DAEMON_UNREACHABLE also counts, and already slept. If a call is killed and prints no PENDING_COUNT, treat it as PENDING_COUNT greater than 0. Count every step 2 call in wait_loops; last_wait_rc is the last WAIT_RC printed.

Report one entry per name, in this order: ${names.join(', ')}. Use the last line printed for a name.
- SKIP: state "skipped", job_id "", rc 0, out_bytes as printed.
- REFUSED: state "refused", job_id "", rc as printed, notes = its error text.
- NOT_SENT: state "not-sent", job_id "", rc as printed, notes = its error text.
- OUTCOME_UNKNOWN: state "submit-unknown", job_id "", rc as printed, notes = its error text.
- DONE: state as printed (succeeded, failed, cancelled or lost), job_id, rc and out_bytes as printed, notes = its export_error text if any.
- PENDING when the loop ended: state "running", job_id, rc 124, out_bytes 0.
- UNKNOWN, or a name with none of these lines: state "unknown", job_id as printed or "", rc -1, out_bytes 0, notes = the error text or the marker that was missing.`

phase('Dispatch')
log(`${items.length} job(s) as batch ${label}, state key ${key}${requestId ? ' (a relaunch dedupes and resumes)' : ' (no requestId: a relaunch submits nothing and prints the reset command)'}; up to ${MAX_WAIT_LOOPS} waits of ${WAIT_S} s, about 90k login tokens each plus twice the manifest text (${manifestText.length} characters)`)

let result = null
try {
  result = await agent(prompt, { label: `dispatch:${label}`, phase: 'Dispatch', model: 'haiku', schema: RESULT_SCHEMA })
} catch (error) {
  result = null
}
if (!result) {
  log(`${label}: the dispatcher agent died; check \`subfleet runs --mine\` and ${stateDir}/${files.jobs} before relaunching`)
  return { label, request_id: requestId || '', state_key: key, state_dir: stateDir, dispatcher: 'died', jobs: [] }
}

const byName = new Map((result.jobs || []).map(row => [row.name, row]))
const jobs = items.map(item => {
  const row = byName.get(item.name)
  if (!row) return { name: item.name, job_id: '', state: 'unknown', rc: -1, out_path: item.outPath, out_bytes: 0, notes: 'the dispatcher reported nothing for this name' }
  return { ...row, out_path: item.outPath }
})
const count = state => jobs.filter(row => row.state === state).length
for (const row of jobs) {
  if (['refused', 'not-sent', 'submit-unknown', 'failed', 'cancelled', 'lost', 'unknown'].includes(row.state)) log(`${row.name}: ${row.state} rc=${row.rc} ${row.notes || ''}`)
  if (row.state === 'running') log(`${row.name}: still pending after ${result.wait_loops} waits; job ${row.job_id} continues in the daemon: subfleet wait ${row.job_id}`)
}
if (!result.setup_ok) log(`${label}: step 0 did not finish; ${result.notes || ''}`)
return {
  label,
  request_id: result.request_id || requestId || '',
  state_key: key,
  state_dir: stateDir,
  setup_ok: result.setup_ok,
  already_submitted: result.already_submitted,
  submit_calls: result.submit_calls,
  submit_rc: result.submit_rc,
  wait_loops: result.wait_loops,
  last_wait_rc: result.last_wait_rc,
  jobs,
  summary: {
    items: items.length, succeeded: count('succeeded'), skipped: count('skipped'),
    refused: count('refused'), unsettled: count('not-sent') + count('submit-unknown'),
    failed: count('failed') + count('cancelled') + count('lost'),
    running_at_cap: count('running'), unknown: count('unknown'),
  },
  notes: result.notes || '',
}
