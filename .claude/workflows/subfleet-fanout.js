export const meta = {
  name: 'subfleet-fanout',
  description: 'Fan jobs out through subfleet: one Haiku dispatcher submits a batch manifest, then loops subfleet wait in 540 s slices and reports every job',
  whenToUse: 'Script control flow over heavy workers. The workers run on subfleet lanes (another account or model); only one thin dispatcher spends this login. args: [{task, tier, dir, promptPath, outPath, name}], or {items, label, requestId, stateDir, maxWaitLoops, allowTmp}. Pass requestId to make a relaunch idempotent. Call it from another script with workflow("subfleet-fanout", args).',
  phases: [
    { title: 'Dispatch', detail: 'one agent: write the manifest, skip items whose outPath is non-empty, subfleet run --batch, then subfleet wait --timeout 540 until the exit code is not 124', model: 'haiku' },
  ],
}

// Why the dispatcher looks like this (subfleet 2.0.0a0, read 2026-09-22):
// - `run --batch FILE --json` submits every manifest entry in one call and
//   prints one JSON object per entry with job_id, created, rc and error
//   (docs/acceptance-contract.md C-17.7; subfleet/cli.py cmd_run_batch).
//   Inside a Claude session the submission returns at once (C-17.6).
// - `--request-id ID` names entry n `ID-n`; resubmitting the same manifest
//   returns the existing jobs instead of new ones (C-6.2), so a relaunch of
//   this workflow that reruns the dispatcher dispatches nothing twice. Without
//   it the only guards are the skip check below and the daemon's output
//   lease: a second job on an -o path a live job holds is refused (C-6.5).
// - The Bash tool caps one call at 600 s, so the wait is sliced:
//   `wait <ids> --timeout 540 --json` prints a JSON object per finished job
//   and {"job_id", "state": "running", "timeout": true} per pending one, and
//   exits 124 while any job is pending (C-15.4, C-17.3; cli.py wait_jobs).
// - The -o file is written only after a deliverable is accepted (C-8.3), so
//   a non-empty outPath means a finished job already delivered; the
//   dispatcher skips such items before submitting.
// - The daemon queues and caps the batch itself (C-6.4, C-6.9); nothing here
//   limits concurrency. Queue time counts against the wait loops.
// - The dispatcher never runs `runs show`: that acknowledges the job's notice
//   for the caller session (C-15.3), and the dispatcher shares the launching
//   session's CLAUDE_CODE_SESSION_ID.

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
// 20 slices of 540 s is 3 h of queue plus run time.
const MAX_WAIT_LOOPS = positiveInt(opts.maxWaitLoops, 20)
const label = opts.label === undefined ? 'fanout' : opts.label
const requestId = opts.requestId === undefined ? null : opts.requestId
const allowTmp = opts.allowTmp === true

const problems = []
const isPath = value => typeof value === 'string' && value.startsWith('/') && !/[\n\r]/.test(value)
if (!LABEL_RE.test(label)) problems.push(`label must match ${LABEL_RE}`)
if (requestId !== null && !LABEL_RE.test(requestId)) problems.push(`requestId must match ${LABEL_RE}`)
if (opts.stateDir !== undefined && !isPath(opts.stateDir)) problems.push('stateDir must be an absolute path')
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
  if (typeof item.name !== 'string' || !LABEL_RE.test(item.name)) problems.push(`${at}: name must match ${LABEL_RE}`)
  if (seenName.has(item.name)) problems.push(`${at}: name ${item.name} is used twice`)
  if (seenOut.has(item.outPath)) problems.push(`${at}: outPath ${item.outPath} is used twice; two jobs would write one file`)
  seenName.add(item.name); seenOut.add(item.outPath)
})
if (problems.length) throw new Error(`subfleet-fanout: ${problems.join('; ')}`)

const stateDir = opts.stateDir || items[0].outPath.slice(0, items[0].outPath.lastIndexOf('/'))
const q = value => `'${String(value).replace(/'/g, `'\\''`)}'`
const HD = 'SUBFLEET_FANOUT_EOF'
const manifest = {
  label,
  jobs: items.map(item => ({ task: item.task, tier: item.tier, workdir: item.dir, prompt: item.promptPath, out: item.outPath, name: item.name })),
}
const manifestJson = JSON.stringify(manifest, null, 1)
if (manifestJson.split('\n').includes(HD)) throw new Error('subfleet-fanout: manifest collides with the heredoc delimiter')
const files = {
  all: `${label}.manifest.all.json`, manifest: `${label}.manifest.json`, submit: `${label}.submit.jsonl`,
  submitErr: `${label}.submit.err`, jobs: `${label}.jobs.txt`, wait: `${label}.wait.jsonl`, waitErr: `${label}.wait.err`,
}
const runFlags = `--json${allowTmp ? ' --allow-tmp' : ''}${requestId ? ` --request-id ${q(requestId)}` : ''}`

// Step 1: manifest, skip check, one `subfleet run --batch`, job ids to a file.
const step1 = [
  `cd ${q(stateDir)} && cat > ${q(files.all)} <<'${HD}'`,
  manifestJson,
  HD,
  `python3 - <<'${HD}'`,
  `import json, os`,
  `F = ${JSON.stringify(files)}`,
  `doc = json.load(open(F['all']))`,
  `keep = []`,
  `for job in doc['jobs']:`,
  `    size = os.path.getsize(job['out']) if os.path.isfile(job['out']) else 0`,
  `    if size > 0:`,
  `        print(f"SKIP name={job['name']} out_bytes={size}")`,
  `    else:`,
  `        keep.append(job)`,
  `json.dump({'label': doc['label'], 'jobs': keep}, open(F['manifest'], 'w'), indent=1)`,
  `open(F['jobs'], 'w').close()`,
  `print(f"TO_SUBMIT={len(keep)}")`,
  HD,
  `if [ "$(python3 -c "import json; print(len(json.load(open(${q(files.manifest)}))['jobs']))")" -gt 0 ]; then`,
  `  subfleet run --batch ${q(files.manifest)} ${runFlags} > ${q(files.submit)} 2> ${q(files.submitErr)}; echo "SUBMIT_RC=$?"`,
  `  python3 - <<'${HD}'`,
  `import json`,
  `F = ${JSON.stringify(files)}`,
  `ids = []`,
  `batch = ''`,
  `for line in open(F['submit']):`,
  `    line = line.strip()`,
  `    if not line: continue`,
  `    row = json.loads(line)`,
  `    batch = row.get('batch') or batch`,
  `    if row.get('job_id'):`,
  `        ids.append(row['job_id'])`,
  `        print(f"SUBMITTED name={row.get('name')} job_id={row['job_id']} created={row.get('created')}")`,
  `    else:`,
  `        print(f"REFUSED name={row.get('name')} rc={row.get('rc')} error={row.get('error')}")`,
  `open(F['jobs'], 'w').write(''.join(i + '\\n' for i in ids))`,
  `print(f"REQUEST_ID={batch if ids else ''}")`,
  HD,
  `  tail -n 3 ${q(files.submitErr)}`,
  `else echo "SUBMIT_RC=0"; echo "NOTHING_TO_SUBMIT"; fi`,
].join('\n')

// Step 2: one 540 s wait slice over every submitted job id.
const step2 = [
  `cd ${q(stateDir)} && if [ -s ${q(files.jobs)} ]; then subfleet wait $(cat ${q(files.jobs)}) --timeout ${WAIT_S} --json > ${q(files.wait)} 2> ${q(files.waitErr)}; echo "WAIT_RC=$?"`,
  `python3 - <<'${HD}'`,
  `import json, os`,
  `F = ${JSON.stringify(files)}`,
  `names = {}`,
  `for line in open(F['submit']):`,
  `    line = line.strip()`,
  `    if line:`,
  `        row = json.loads(line)`,
  `        if row.get('job_id'): names[row['job_id']] = row.get('name')`,
  `for line in open(F['wait']):`,
  `    line = line.strip()`,
  `    if not line: continue`,
  `    o = json.loads(line)`,
  `    jid = o.get('job_id'); name = o.get('name') or names.get(jid)`,
  `    if o.get('timeout'):`,
  `        print(f"PENDING name={name} job_id={jid} state={o.get('state')} waited_s={o.get('waited_s')}")`,
  `    else:`,
  `        out = o.get('out_path')`,
  `        size = os.path.getsize(out) if out and os.path.isfile(out) else 0`,
  `        print(f"DONE name={name} job_id={jid} state={o.get('state')} rc={o.get('rc')} out_bytes={size}")`,
  HD,
  `tail -n 2 ${q(files.waitErr)}; else echo "WAIT_RC=0"; echo "NOTHING_TO_WAIT"; fi`,
].join('\n')

const RESULT_SCHEMA = {
  type: 'object',
  properties: {
    submit_rc: { type: 'integer', description: 'the SUBMIT_RC number from step 1' },
    request_id: { type: 'string', description: 'the REQUEST_ID value from step 1; empty when nothing was submitted' },
    wait_loops: { type: 'integer', description: 'how many step 2 calls were made' },
    last_wait_rc: { type: 'integer', description: 'the WAIT_RC number of the last step 2 call; 124 when the wait cap was reached' },
    jobs: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          name: { type: 'string' },
          job_id: { type: 'string', description: 'empty when skipped or refused' },
          state: { type: 'string', enum: ['skipped', 'refused', 'succeeded', 'failed', 'cancelled', 'lost', 'running'] },
          rc: { type: 'integer', description: 'the DONE line rc; 0 when skipped; the REFUSED rc; 124 when still running at the wait cap' },
          out_bytes: { type: 'integer' },
          notes: { type: 'string', description: 'the REFUSED error text, or empty' },
        },
        required: ['name', 'job_id', 'state', 'rc', 'out_bytes', 'notes'],
      },
    },
    notes: { type: 'string', description: 'anything unexpected: a step whose marker lines were missing, a killed call, stderr tails worth keeping; else empty' },
  },
  required: ['submit_rc', 'request_id', 'wait_loops', 'last_wait_rc', 'jobs', 'notes'],
}

const names = items.map(item => item.name)
const prompt = `You are one dispatcher inside a workflow. The main session is already carrying out the user's request shown above, as a whole. It launched this workflow as one step of that work and gave you one mechanical job, so this job does not conflict with the user's request: it is the part of it assigned to you. Do not start the user's request yourself. Do not read its files, load skills, write or edit anything, or commit; the main session does all of that, and a second copy of the work would collide with it.

Run only the Bash commands below, exactly as written, with the Bash tool's timeout parameter set to ${BASH_TIMEOUT_MS} on every call. Do not run any other subfleet subcommand (no status, runs, kill, pick, why, sessions, gate, codex, claude) and never submit twice.

STEP 1. One Bash call:
${step1}

Note every SKIP, SUBMITTED and REFUSED line, plus SUBMIT_RC and REQUEST_ID. If the output says NOTHING_TO_SUBMIT, or has no SUBMITTED line, skip step 2 and report with wait_loops 0 and last_wait_rc 0.

STEP 2. One Bash call per loop:
${step2}

- WAIT_RC=124, or no WAIT_RC line at all (the call was killed): some job is still going. Make the same call again. Count every step 2 call in wait_loops. After ${MAX_WAIT_LOOPS} calls stop: each job still on a PENDING line is state "running" with rc 124.
- Any other WAIT_RC: done. Each DONE line gives that job's state, rc and out_bytes.

Report one entry per name, in this order: ${names.join(', ')}. A SKIP line is state "skipped", job_id "", rc 0. A REFUSED line is state "refused", job_id "", rc as printed, notes = its error text. Copy numbers exactly.`

phase('Dispatch')
log(`${items.length} job(s) as batch ${label}${requestId ? ` (request id ${requestId}, idempotent)` : ' (no requestId: a relaunch that reruns the dispatcher submits again)'}; up to ${MAX_WAIT_LOOPS} waits of ${WAIT_S} s`)

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
  if (!row) return { name: item.name, job_id: '', state: 'unreported', rc: null, out_path: item.outPath, out_bytes: 0, notes: 'the dispatcher reported nothing for this name' }
  return { ...row, out_path: item.outPath }
})
const count = state => jobs.filter(row => row.state === state).length
for (const row of jobs) {
  if (['refused', 'failed', 'cancelled', 'lost', 'unreported'].includes(row.state)) log(`${row.name}: ${row.state} rc=${row.rc} ${row.notes || ''}`)
  if (row.state === 'running') log(`${row.name}: still running after ${result.wait_loops} waits; job ${row.job_id} continues in the daemon: subfleet wait ${row.job_id}`)
}
return {
  label,
  request_id: result.request_id || requestId || '',
  state_dir: stateDir,
  submit_rc: result.submit_rc,
  wait_loops: result.wait_loops,
  last_wait_rc: result.last_wait_rc,
  jobs,
  summary: {
    items: items.length, succeeded: count('succeeded'), skipped: count('skipped'),
    refused: count('refused'), failed: count('failed') + count('cancelled') + count('lost'),
    running_at_cap: count('running'), unreported: count('unreported'),
  },
  notes: result.notes || '',
}
