export const meta = {
  name: 'subfleet-fanout',
  description: 'Fan jobs out through subfleet: one Haiku dispatcher submits a batch manifest, then loops subfleet wait in 540 s slices until no job is pending and reports every job',
  whenToUse: 'Script control flow over heavy workers. The workers run on subfleet lanes (another account or model); only one thin dispatcher spends this login. Check `subfleet status` first: a queued batch costs about 90,000 login tokens per 540 s wait slice. args: [{task, tier, dir, promptPath, outPath, name, model?, sandbox?}], or {items, label, requestId, stateDir, maxWaitLoops, allowTmp, model?, sandbox?} (top-level model/sandbox are defaults for every item). Pass requestId to make a relaunch idempotent. Call it from another script with workflow("subfleet-fanout", args).',
  phases: [
    { title: 'Dispatch', detail: 'one agent: write a helper and the manifest, subfleet run --batch once, then subfleet wait --timeout 540 until PENDING_COUNT is 0', model: 'haiku' },
  ],
}

// Why the dispatcher looks like this (subfleet 2.0.0a0, read 2026-09-22 and
// 2026-10-04; measured run and review findings in
// docs/reports/2026-09-22-fanout-workflow.md):
// - `run --batch FILE --json` submits every manifest entry in one call and
//   prints one JSON object per entry with job_id, created, rc and error
//   (docs/acceptance-contract.md C-17.7; subfleet/cli.py cmd_run_batch).
//   Inside a Claude session the submission returns at once (C-17.6).
// - `--request-id ID` names entry n `ID-n`; resubmitting the same manifest
//   returns the existing jobs instead of new ones (C-6.2), so a relaunch of
//   this workflow that reruns the dispatcher dispatches nothing twice. The
//   index is part of the id, so with a request id the FULL manifest is always
//   submitted and the daemon dedupes; dropping delivered items would shift
//   every later entry onto another entry's id (review finding 1). Without a
//   request id, items whose outPath is already non-empty are dropped before
//   submission as a heuristic: the daemon never clears a stale -o file.
// - A step never truncates its own record: the helper refuses to submit when
//   a jobs file or submit log already exists, and job ids are written only
//   after the submit log is parsed (review finding 2).
// - The Bash tool caps one call at 600 s, so the wait is sliced:
//   `wait <ids> --timeout 540 --json` prints a JSON object per finished job
//   and {"job_id", "state": "running", "timeout": true} per pending one, and
//   exits 124 while any job is pending (C-15.4, C-17.3; cli.py wait_jobs).
//   The daemon answers a multi-job wait only when every job is terminal and
//   exported (daemon.py _wait_answer), so the loop is driven by the count of
//   pending rows, never by the exit code: a lost (125) or cancelled (130) job
//   outranks 124 in the CLI's max() (review finding 3). Exit 1 or 69 means the
//   daemon did not answer; the helper sleeps 60 s and the loop continues, so a
//   daemon restart costs one slice, not the whole loop (a 2026-09-24 run burnt
//   19 slices in seconds that way).
// - Each 540 s slice outlives the prompt cache's 5-minute lifetime, so every
//   slice re-writes the dispatcher's whole context (about 45,000 tokens, twice
//   per slice, observed 2026-09-22). Queue time counts: with no open lane a
//   batch waited 3.7 h before its first job started.
// - The dispatcher never runs `runs show`: that acknowledges the job's notice
//   for the caller session (C-15.3, cli.py _ack_notices), and the dispatcher
//   shares the launching session's CLAUDE_CODE_SESSION_ID.

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
const isWord = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(value)
if (!LABEL_RE.test(label)) problems.push(`label must match ${LABEL_RE}`)
if (requestId !== null && !LABEL_RE.test(requestId)) problems.push(`requestId must match ${LABEL_RE}`)
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

// Every state file carries the request id when there is one, so two batches
// sharing a directory never read each other's job ids (review finding 6).
const key = requestId || label
const stateDir = opts.stateDir || items[0].outPath.slice(0, items[0].outPath.lastIndexOf('/'))
const q = value => `'${String(value).replace(/'/g, `'\\''`)}'`
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
const files = {
  helper: `${key}.fanout.py`, all: `${key}.manifest.all.json`, manifest: `${key}.manifest.json`,
  submit: `${key}.submit.jsonl`, submitErr: `${key}.submit.err`, jobs: `${key}.jobs.txt`,
  wait: `${key}.wait.jsonl`, waitErr: `${key}.wait.err`,
}
const HD = 'FANOUT_PY_EOF'
// JSON.stringify twice: the inner call is the manifest, the outer makes it a
// double-quoted string literal whose escapes Python reads the same way.
const helper = `import json, os, sys, time
KEY = ${JSON.stringify(key)}
REQUEST_ID = ${JSON.stringify(requestId || '')}
MANIFEST = json.loads(${JSON.stringify(JSON.stringify(manifest))})
F = ${JSON.stringify(files)}
NL = chr(10)
RETRY_S = int(os.environ.get("SUBFLEET_FANOUT_RETRY_S", "60"))

def size(path):
    try:
        return os.path.getsize(path) if os.path.isfile(path) else 0
    except OSError:
        return 0

def lines(path):
    if not os.path.isfile(path):
        return []
    with open(path) as fh:
        return [line.strip() for line in fh if line.strip()]

def rows(path, kind):
    out = []
    for line in lines(path):
        try:
            out.append(json.loads(line))
        except ValueError:
            print("BAD_" + kind + "_LINE " + line[:160])
    return out

def tail(path, n=3):
    return " | ".join(lines(path)[-n:])

def write_jobs(ids):
    with open(F["jobs"], "w") as fh:
        fh.write(NL.join(ids) + NL if ids else "")

def cmd_setup():
    with open(F["all"], "w") as fh:
        json.dump(MANIFEST, fh, indent=1)
    prior = lines(F["jobs"]) or [r["job_id"] for r in rows(F["submit"], "SUBMIT") if r.get("job_id")]
    if prior:
        write_jobs(prior)
        print("ALREADY_SUBMITTED jobs=" + str(len(prior)))
        print("SETUP_OK")
        return
    keep = []
    for job in MANIFEST["jobs"]:
        n = size(job["out"])
        if n > 0 and not REQUEST_ID:
            print("SKIP name=" + job["name"] + " out_bytes=" + str(n))
            continue
        if n > 0:
            print("NOTE name=" + job["name"] + " out exists (" + str(n) + " bytes); submitted anyway, the request id dedupes")
        keep.append(job)
    with open(F["manifest"], "w") as fh:
        json.dump({"label": MANIFEST["label"], "jobs": keep}, fh, indent=1)
    print("TO_SUBMIT=" + str(len(keep)))
    print("SETUP_OK")

def cmd_should_submit():
    if lines(F["jobs"]) or rows(F["submit"], "SUBMIT"):
        print("ALREADY_SUBMITTED")
        sys.exit(1)
    if not os.path.isfile(F["manifest"]):
        print("SETUP_MISSING")
        sys.exit(1)
    with open(F["manifest"]) as fh:
        count = len(json.load(fh)["jobs"])
    if count == 0:
        print("NOTHING_TO_SUBMIT")
        sys.exit(1)
    sys.exit(0)

def cmd_post_submit():
    submitted = rows(F["submit"], "SUBMIT")
    ids, batch = [], ""
    for row in submitted:
        batch = row.get("batch") or batch
        if row.get("job_id"):
            ids.append(row["job_id"])
            print("SUBMITTED name=%s job_id=%s created=%s" % (row.get("name"), row["job_id"], row.get("created")))
        else:
            print("REFUSED name=%s rc=%s error=%s" % (row.get("name"), row.get("rc"), row.get("error")))
    existing = lines(F["jobs"])
    merged = existing + [i for i in ids if i not in existing]
    write_jobs(merged)
    if not submitted and os.path.isfile(F["submitErr"]):
        print("SUBMIT_ERR " + tail(F["submitErr"]))
    print("REQUEST_ID=" + (batch if merged else ""))
    print("JOBS=" + str(len(merged)))

def cmd_post_wait(rc):
    print("WAIT_RC=" + str(rc))
    names = {r["job_id"]: r.get("name") for r in rows(F["submit"], "SUBMIT") if r.get("job_id")}
    outs = {job["name"]: job["out"] for job in MANIFEST["jobs"]}
    ids = lines(F["jobs"])
    seen, pending = set(), 0
    for o in rows(F["wait"], "WAIT"):
        jid = o.get("job_id")
        name = o.get("name") or names.get(jid) or ""
        seen.add(jid)
        if o.get("timeout"):
            pending += 1
            print("PENDING name=%s job_id=%s waited_s=%s" % (name, jid, o.get("waited_s")))
        else:
            out = o.get("out_path") or outs.get(name) or ""
            print("DONE name=%s job_id=%s state=%s rc=%s out_bytes=%s export_error=%s"
                  % (name, jid, o.get("state"), o.get("rc"), size(out), o.get("export_error") or ""))
    missing = [jid for jid in ids if jid not in seen]
    if rc in (1, 69):
        print("DAEMON_UNREACHABLE rc=%s err=%s" % (rc, tail(F["waitErr"])))
        for jid in missing:
            pending += 1
            print("PENDING name=%s job_id=%s waited_s=0" % (names.get(jid, ""), jid))
        time.sleep(RETRY_S)
        print("RETRY_SLEPT=" + str(RETRY_S))
    else:
        for jid in missing:
            print("UNKNOWN name=%s job_id=%s rc=%s error=%s" % (names.get(jid, ""), jid, rc, tail(F["waitErr"])))
    print("PENDING_COUNT=" + str(pending))

verb = sys.argv[1] if len(sys.argv) > 1 else ""
if verb == "setup":
    cmd_setup()
elif verb == "should-submit":
    cmd_should_submit()
elif verb == "post-submit":
    cmd_post_submit()
elif verb == "post-wait":
    cmd_post_wait(int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].lstrip("-").isdigit() else -1)
else:
    print("BAD_VERB " + verb)
    sys.exit(2)
`
if (helper.split('\n').includes(HD)) throw new Error('subfleet-fanout: helper collides with the heredoc delimiter')

const runFlags = `--json${allowTmp ? ' --allow-tmp' : ''}${requestId ? ` --request-id ${q(requestId)}` : ''}`
const step0 = `mkdir -p ${q(stateDir)} && cd ${q(stateDir)} && cat > ${q(files.helper)} <<'${HD}'\n${helper}${HD}\npython3 ${q(files.helper)} setup`
const step1 = `cd ${q(stateDir)} && if python3 ${q(files.helper)} should-submit; then subfleet run --batch ${q(files.manifest)} ${runFlags} > ${q(files.submit)} 2> ${q(files.submitErr)}; echo "SUBMIT_RC=$?"; fi; python3 ${q(files.helper)} post-submit`
const step2 = `cd ${q(stateDir)} && if [ -s ${q(files.jobs)} ]; then subfleet wait $(cat ${q(files.jobs)}) --timeout ${WAIT_S} --json > ${q(files.wait)} 2> ${q(files.waitErr)}; python3 ${q(files.helper)} post-wait $?; else echo "NOTHING_TO_WAIT"; echo "PENDING_COUNT=0"; fi`

const RESULT_SCHEMA = {
  type: 'object',
  properties: {
    setup_ok: { type: 'boolean', description: 'true when step 0 printed SETUP_OK' },
    already_submitted: { type: 'boolean', description: 'true when step 0 or step 1 printed ALREADY_SUBMITTED' },
    submit_rc: { type: 'integer', description: 'the SUBMIT_RC number from step 1; 0 when no submission ran' },
    request_id: { type: 'string', description: 'the REQUEST_ID value from step 1; empty when nothing was submitted' },
    wait_loops: { type: 'integer', description: 'how many step 2 calls were made' },
    last_wait_rc: { type: 'integer', description: 'the WAIT_RC number of the last step 2 call; 0 when step 2 never ran' },
    jobs: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          job_id: { type: 'string', description: 'empty when skipped, refused or unknown' },
          state: { type: 'string', enum: ['skipped', 'refused', 'succeeded', 'failed', 'cancelled', 'lost', 'running', 'unknown'] },
          rc: { type: 'integer', description: 'the DONE line rc; 0 when skipped; the REFUSED rc; 124 when still pending at the loop cap; -1 when unknown' },
          out_bytes: { type: 'integer' },
          notes: { type: 'string', description: 'the REFUSED error, the DONE export_error, the UNKNOWN error, or empty' },
        },
        required: ['name', 'job_id', 'state', 'rc', 'out_bytes', 'notes'],
      },
    },
    notes: { type: 'string', description: 'anything unexpected: a step whose marker lines were missing, a killed call, DAEMON_UNREACHABLE or SUBMIT_ERR lines, BAD_*_LINE lines; else empty' },
  },
  required: ['setup_ok', 'already_submitted', 'submit_rc', 'request_id', 'wait_loops', 'last_wait_rc', 'jobs', 'notes'],
}

const names = items.map(item => item.name)
const prompt = `You are one dispatcher inside a workflow. The main session is already carrying out the user's request shown above, as a whole. It launched this workflow as one step of that work and gave you one mechanical job, so this job does not conflict with the user's request: it is the part of it assigned to you. Do not start the user's request yourself. Do not read its files, load skills, write or edit anything, or commit; the main session does all of that, and a second copy of the work would collide with it.

Run only the Bash commands below, exactly as written, character for character, with the Bash tool's timeout parameter set to ${BASH_TIMEOUT_MS} on every call. Do not run any other subfleet subcommand (no status, runs, kill, pick, why, sessions, gate, codex, claude) and never submit twice. Each command prints marker lines in CAPITALS; copy their numbers exactly.

STEP 0. One Bash call. It writes a helper script and prints SETUP_OK:
${step0}

STEP 1. One Bash call. It submits the batch unless one was already submitted, and prints SUBMITTED, REFUSED, SKIP, SUBMIT_RC, REQUEST_ID and JOBS lines:
${step1}

If JOBS=0, skip step 2.

STEP 2. One Bash call per loop. It waits up to 540 s and prints DONE, PENDING or UNKNOWN lines and PENDING_COUNT:
${step2}

Repeat step 2 while PENDING_COUNT is greater than 0, and stop after ${MAX_WAIT_LOOPS} calls in total. A call that prints DAEMON_UNREACHABLE also counts, and already slept 60 s. If a call is killed and prints no PENDING_COUNT, treat it as PENDING_COUNT greater than 0. Count every step 2 call in wait_loops; last_wait_rc is the last WAIT_RC printed.

Report one entry per name, in this order: ${names.join(', ')}.
- A SKIP line: state "skipped", job_id "", rc 0, out_bytes as printed.
- A REFUSED line: state "refused", job_id "", rc as printed, notes = its error text.
- A DONE line: state as printed (succeeded, failed, cancelled or lost), job_id, rc and out_bytes as printed, notes = its export_error text if any.
- A PENDING line when the loop ended: state "running", job_id, rc 124, out_bytes 0.
- An UNKNOWN line, or a name with none of these lines: state "unknown", job_id as printed or "", rc -1, out_bytes 0, notes = the error text or the marker that was missing.`

phase('Dispatch')
log(`${items.length} job(s) as batch ${label}${requestId ? ` (request id ${requestId}: a relaunch dedupes)` : ' (no requestId: a relaunch submits any item whose output is still empty)'}; up to ${MAX_WAIT_LOOPS} waits of ${WAIT_S} s, about 90k login tokens each`)

let result = null
try {
  result = await agent(prompt, { label: `dispatch:${label}`, phase: 'Dispatch', model: 'haiku', schema: RESULT_SCHEMA })
} catch (error) {
  result = null
}
if (!result) {
  log(`${label}: the dispatcher agent died; check \`subfleet runs --mine\` and ${stateDir}/${files.jobs} before relaunching`)
  return { label, request_id: requestId || '', state_dir: stateDir, dispatcher: 'died', jobs: [] }
}

const byName = new Map((result.jobs || []).map(row => [row.name, row]))
const jobs = items.map(item => {
  const row = byName.get(item.name)
  if (!row) return { name: item.name, job_id: '', state: 'unknown', rc: -1, out_path: item.outPath, out_bytes: 0, notes: 'the dispatcher reported nothing for this name' }
  return { ...row, out_path: item.outPath }
})
const count = state => jobs.filter(row => row.state === state).length
for (const row of jobs) {
  if (['refused', 'failed', 'cancelled', 'lost', 'unknown'].includes(row.state)) log(`${row.name}: ${row.state} rc=${row.rc} ${row.notes || ''}`)
  if (row.state === 'running') log(`${row.name}: still pending after ${result.wait_loops} waits; job ${row.job_id} continues in the daemon: subfleet wait ${row.job_id}`)
}
if (!result.setup_ok) log(`${label}: step 0 did not print SETUP_OK; ${result.notes || ''}`)
return {
  label,
  request_id: result.request_id || requestId || '',
  state_dir: stateDir,
  setup_ok: result.setup_ok,
  already_submitted: result.already_submitted,
  submit_rc: result.submit_rc,
  wait_loops: result.wait_loops,
  last_wait_rc: result.last_wait_rc,
  jobs,
  summary: {
    items: items.length, succeeded: count('succeeded'), skipped: count('skipped'),
    refused: count('refused'), failed: count('failed') + count('cancelled') + count('lost'),
    running_at_cap: count('running'), unknown: count('unknown'),
  },
  notes: result.notes || '',
}
