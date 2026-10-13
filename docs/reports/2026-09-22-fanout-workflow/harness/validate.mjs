import assert from 'node:assert/strict'
import { execFileSync, spawn, spawnSync } from 'node:child_process'
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, unlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { renderWorkflow, runWorkflow } from './render.mjs'

const script = resolve(process.argv[2])
const harness = dirname(fileURLToPath(import.meta.url))
const run = (args, agentImpl) => runWorkflow(script, args, agentImpl)
// A shared host can run at load 50-100: steps, barriers and replays get
// minutes, not seconds. A LOCKED peer is rerun, as a dispatcher relaunch would.
const STEP_TIMEOUT_MS = 240000, BARRIER_MS = 90000
let failures = 0, passed = 0, lockedPeers = 0
const test = async (name, fn) => {
  if (process.env.FANOUT_TEST_FILTER && !name.includes(process.env.FANOUT_TEST_FILTER)) return
  try { await fn(); passed++; console.log('PASS', name) }
  catch (e) { failures++; console.error('FAIL', name, e.message.slice(0, 1000)) }
}

const ok = { task: 'review', tier: 'standard', dir: '/Users/x', promptPath: '/p.md', outPath: '/o/a.md', name: 'a' }
const cases = [
  [[], 'empty'], ['x', 'string'],
  [[{ ...ok, task: 'nope' }], 'bad task'], [[{ ...ok, dir: 'rel' }], 'relative dir'], [[{ ...ok, dir: '/tmp/x' }], 'tmp dir'],
  [[{ ...ok, name: 'bad name' }], 'bad name'], [[ok, ok], 'dup'], [{ items: [ok], label: 'bad label' }, 'bad label'],
  [{ items: [ok], requestId: 'has space' }, 'bad requestId'], [{ items: [{ ...ok, sandbox: 'rw' }] }, 'bad sandbox'],
  [{ items: [{ ...ok, model: 'gpt 6' }] }, 'bad model'],
]
await test('argument validation (11 inputs)', async () => {
  for (const [bad] of cases) await assert.rejects(() => run(bad, async () => null), /subfleet-fanout:/)
})
await test('allowTmp, model defaults, dead agent and result shaping', async () => {
  const rendered = await renderWorkflow(script, { items: [{ ...ok, dir: '/tmp/x' }], allowTmp: true, model: 'opus' })
  assert.ok(/--allow-tmp/.test(rendered.commands[1]) || /ALLOW_TMP = True/.test(rendered.commands[0]))
  assert.match(rendered.commands[0], /"model":"opus"/)
  const r = await run({ items: [{ ...ok, dir: '/tmp/x' }], allowTmp: true, model: 'opus' }, async () => ({ setup_ok: true, jobs: [] }))
  assert.equal(r.out.jobs[0].state, 'unknown')
  const dead = await run({ items: [ok] }, async () => null)
  assert.equal(dead.out.dispatcher, 'died')
  const full = await run({ items: [ok, { ...ok, name: 'b', outPath: '/o/b.md' }, { ...ok, name: 'c', outPath: '/o/c.md' }] }, async () => ({ setup_ok: true,
    jobs: [{ name: 'a', job_id: 'ja', state: 'succeeded', rc: 0 }, { name: 'c', job_id: 'jc', state: 'lost', rc: 125 }] }))
  assert.equal(full.out.jobs[1].state, 'unknown')
  assert.equal(full.out.jobs[0].out_path, '/o/a.md')
  assert.equal(full.out.summary.succeeded, 1)
  assert.equal(full.out.summary.unknown, 1)
})

// Commands cross the review's zsh eval boundary. The fake is first in PATH;
// SUBFLEET_HOME is empty. Generated artifacts stay in this temporary folder.
const temp = mkdtempSync(join(tmpdir(), 'fanout-r5-'))
const make = async (scenario = '', requestId = 'rq-test', stateSuffix = 'state') => {
  const root = mkdtempSync(join(temp, 'case-'))
  mkdirSync(join(root, 'bin'))
  mkdirSync(join(root, 'empty-home'))
  copyFileSync(join(harness, 'fake-subfleet.sh'), join(root, 'bin', 'subfleet'))
  chmodSync(join(root, 'bin', 'subfleet'), 0o755)
  // A fake git supplies only the commit inspected by the generated helper.
  writeFileSync(join(root, 'bin', 'git'), '#!/bin/bash\nprintf "%s\\n" "${FAKE_WORKDIR_HEAD:-head-one}"\n')
  chmodSync(join(root, 'bin', 'git'), 0o755)
  const env = { ...process.env, PATH: `${root}/bin:${process.env.PATH}`, FAKE_STATE: root,
    SCENARIO: scenario, SUBFLEET_HOME: join(root, 'empty-home'), SUBFLEET_FANOUT_RETRY_S: '0', SUBFLEET_RUN_DETACH: '0',
    CLAUDE_CODE_SESSION_ID: 'fanout-harness-caller', ZDOTDIR: join(root, 'empty-home'),
    FANOUT_FAKE_REPOSITORY: resolve(harness, '../../../..') }
  delete env.PYTHONPATH
  const args = { label: 't', stateDir: join(root, stateSuffix), items: ['a', 'b', 'c'].map(name => ({ ...ok, name,
    promptPath: join(root, `prompt-${name}.md`), outPath: join(root, `out-${name}.md`) })) }
  for (const item of args.items) writeFileSync(item.promptPath, `Review ${item.name}`)
  if (requestId !== null) args.requestId = requestId
  const rendered = await renderWorkflow(script, args)
  const shell = command => spawnSync('zsh', ['-c', 'eval "$1"', 'fanout-harness', command], { cwd: root, env, encoding: 'utf8', timeout: STEP_TIMEOUT_MS })
  const step = (i, command = rendered.commands[i]) => {
    const r = shell(command)
    if (r.error) throw r.error
    return { status: r.status, text: r.stdout + r.stderr }
  }
  const count = verb => existsSync(join(root, `calls.${verb}`)) ? Number(readFileSync(join(root, `calls.${verb}`), 'utf8')) : 0
  const setup = () => { const r = step(0); assert.equal(r.status, 0, r.text); assert.match(r.text, /CHECKSUM_OK[\s\S]*SETUP_OK/); return r }
  const submit = () => {
    const max = Number(rendered.prompt.match(/at most (\d+) step 1 calls in total/)[1])
    assert.equal(max, 3)
    const outputs = []
    do { const r = step(1); assert.equal(r.status, 0, r.text); outputs.push(r.text) }
    while (outputs.length < max && /^RETRY_STEP1$/m.test(outputs.at(-1)))
    return outputs
  }
  const wait = () => {
    const outputs = []
    do { const r = step(2); assert.equal(r.status, 0, r.text); outputs.push(r.text) }
    while (outputs.length < 4 && !/^PENDING_COUNT=0$/m.test(outputs.at(-1)))
    assert.match(outputs.at(-1), /^PENDING_COUNT=0$/m)
    return outputs.join('\n')
  }
  return { root, args, env, ...rendered, shell, step, count, setup, submit, wait }
}

try {
  // Round 8. The CLI re-sends after a lost answer (client.py call_settled);
  // when HEAD or policy moved in between, _settle_refused_submit lets the
  // refusal stand and names the job that holds the request id, which this
  // invocation's first send created. Executed by the reviewer with the real
  // cmd_run_batch: {"outcome": "refused", "rc": 2, "error": "request id
  // already used with a different payload by job job-c"}.
  const ownRow = (c, log, name) => readFileSync(join(c.args.stateDir, log), 'utf8').trim().split('\n').map(JSON.parse).find(row => row.name === name)
  for (const moves of ['head', 'policy']) {
    await test(`round 8 finding 1: a refused re-send naming this invocation's job is adopted (${moves} moved, conflict text)`, async () => {
      const c = await make('resend-moved-c,check-prompts'); c.env.FAKE_RESEND_MOVES = moves
      c.env.FAKE_CAPTURE_PAYLOADS = join(c.root, 'actual-payloads.jsonl')
      c.setup()
      const first = c.step(1).text
      const row = ownRow(c, 'rq-test.submit.1.jsonl', 'c')
      assert.deepEqual({ job_id: row.job_id, outcome: row.outcome, rc: row.rc, error: row.error },
        { job_id: null, outcome: 'refused', rc: 2, error: 'request id already used with a different payload by job job-c' })
      const registry = JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))
      const staged = JSON.parse(readFileSync(join(c.args.stateDir, 'rq-test.submit.1.jsonl.manifest.json'), 'utf8'))
      assert.equal(registry['rq-test-3'].batch.label, staged.label, 'the first send created job-c under this invocation')
      const actual = readFileSync(c.env.FAKE_CAPTURE_PAYLOADS, 'utf8').trim().split('\n').map(JSON.parse).filter(p => p.request_id === 'rq-test-3')
      assert.equal(actual.length, 2)
      assert.notEqual(actual[1].payload_digest, registry['rq-test-3'].payload_digest, 'the re-send is a different payload')
      assert.match(first, /^SUBMITTED name=c job_id=job-c created=False$/m)
      assert.match(first, /JOBS=3\nUNSETTLED=0/)
      assert.doesNotMatch(first, /^REFUSED |RETRY_STEP1|LOOKUP_UNKNOWN/m)
      assert.equal(c.count('show'), 1)
      assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
      c.setup(); assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
      assert.equal(c.count('run'), 1)
      assert.equal(c.count('show'), 1)
    })
  }
  await test('round 8 finding 1: the "; job X carries request id R" refusal form is a candidate too', async () => {
    const c = await make('resend-invalid-c,check-prompts'); c.setup()
    const first = c.step(1).text
    assert.match(ownRow(c, 'rq-test.submit.1.jsonl', 'c').error, /; job job-c carries request id rq-test-3$/)
    assert.match(first, /^SUBMITTED name=c job_id=job-c created=False$/m)
    assert.match(first, /JOBS=3\nUNSETTLED=0/)
    assert.doesNotMatch(first, /^REFUSED /m)
    assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
  })
  await test('round 8 finding 1: an adopted re-send survives a retry for another unknown and a relaunch', async () => {
    const c = await make('resend-moved-c,unknown-a,check-prompts'); c.setup()
    const outputs = c.submit()
    assert.equal(outputs.length, 2)
    assert.match(outputs[0], /OUTCOME_UNKNOWN name=a[\s\S]*SUBMITTED name=c job_id=job-c created=False[\s\S]*JOBS=2\nUNSETTLED=1\n[\s\S]*RETRY_STEP1/)
    assert.match(outputs[1], /SUBMITTED name=a job_id=job-a created=False[\s\S]*SUBMITTED name=c job_id=job-c[\s\S]*JOBS=3\nUNSETTLED=0/)
    assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
    c.setup(); assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
    assert.equal(c.count('run'), 2)
  })
  await test('round 8: a refused re-send naming an older state\'s job stays REFUSED', async () => {
    for (const moves of ['head', 'policy']) {
      const c = await make('check-prompts')
      const old = await renderWorkflow(script, { ...c.args, stateDir: join(c.root, 'old-state') })
      c.step(0, old.commands[0]); assert.match(c.step(1, old.commands[1]).text, /JOBS=3\nUNSETTLED=0/)
      const before = readFileSync(join(c.root, 'registry.json'), 'utf8')
      c.env.SCENARIO = 'resend-moved,check-prompts'; c.env.FAKE_RESEND_MOVES = moves
      c.setup()
      for (const output of [c.step(1).text, (c.setup(), c.step(1).text)]) {
        assert.equal((output.match(/^REFUSED name=\w rc=2 error=request id already used with a different payload by job job-\w$/gm) || []).length, 3, output)
        assert.equal((output.match(/^LOOKUP_UNKNOWN name=\w .*created by another invocation.*stays REFUSED$/gm) || []).length, 3, output)
        assert.match(output, /JOBS=0\nUNSETTLED=0/)
        assert.doesNotMatch(output, /^SUBMITTED |RETRY_STEP1/m)
      }
      assert.match(c.step(2).text, /NOTHING_TO_WAIT/)
      assert.equal(readFileSync(join(c.root, 'registry.json'), 'utf8'), before)
      assert.equal(c.count('wait'), 0)
    }
  })
  for (const model of ['astra', 'sol']) {
    await test(`round 8 finding 2: ${model} is submitted as written while the daemon retires astra to sol61`, async () => {
      const c = await make('rowless-unknown,check-prompts'); c.env.FAKE_POLICY_RETIRE = 'astra:sol61'
      c.args.model = model
      const rendered = await renderWorkflow(script, c.args)
      c.step(0, rendered.commands[0])
      assert.match(c.step(1, rendered.commands[1]).text, /JOBS=0\nUNSETTLED=3/)
      const staged = JSON.parse(readFileSync(join(c.args.stateDir, 'rq-test.submit.1.jsonl.manifest.json'), 'utf8'))
      const snapshots = JSON.parse(readFileSync(join(c.args.stateDir, 'rq-test.submit.1.jsonl.identity.json'), 'utf8'))
      const registry = JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))
      for (const [index, name] of ['a', 'b', 'c'].entries()) {
        assert.equal(staged.jobs[index].model, model, 'the CLI accepts only its MODEL_CHOICES')
        assert.equal(snapshots[name].pinned_model, 'sol61')
        assert.equal(snapshots[name].model_source, 'preview')
        assert.equal(registry[`rq-test-${index + 1}`].pinned_model, 'sol61')
      }
      unlinkSync(c.args.items[1].promptPath)
      assert.match(c.step(1, rendered.commands[1]).text, /MANIFEST_REFUSED[\s\S]*JOBS=3\nUNSETTLED=0/)
      c.step(0, rendered.commands[0]); assert.match(c.step(1, rendered.commands[1]).text, /JOBS=3\nUNSETTLED=0/)
      assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
      const direct = await make('check-prompts'); direct.env.FAKE_POLICY_RETIRE = 'astra:sol61'
      const plain = await renderWorkflow(script, { ...direct.args, model })
      direct.step(0, plain.commands[0])
      const submitted = direct.step(1, plain.commands[1]).text
      assert.match(submitted, /JOBS=3\nUNSETTLED=0/)
      assert.doesNotMatch(submitted, /MANIFEST_REFUSED|^REFUSED /m)
    })
  }
  const holdLock = async (c, record) => {
    // A bare holder for status checks: it locks, then the record is rewritten.
    const holder = spawn('python3', ['-c', 'import fcntl,sys,time\nf=open(sys.argv[1],"a+")\nfcntl.flock(f,fcntl.LOCK_EX)\nprint("held",flush=True)\ntime.sleep(120)', join(c.args.stateDir, 'rq-test.lock')], { env: c.env })
    await new Promise((resolveHeld, reject) => { holder.stdout.once('data', resolveHeld); holder.on('error', reject) })
    writeFileSync(join(c.args.stateDir, 'rq-test.lock'), record)
    return holder
  }
  await test('round 8 finding 3: LOCKED names the live CLI child and the killed launcher and helper', async () => {
    const c = await make('check-prompts'); c.setup()
    const realPython = execFileSync('python3', ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8' }).trim()
    const fake = join(c.root, 'bin', 'subfleet'); copyFileSync(fake, fake + '-registry')
    writeFileSync(fake, `#!${realPython}
import json,os,pathlib,sys,time
root=pathlib.Path(os.environ["FAKE_STATE"])
if sys.argv[1] == "run" and "--batch" in sys.argv:
    (root/"holders.new").write_text(json.dumps({"cli":os.getpid(),"helper":os.getppid()}))
    os.replace(root/"holders.new",root/"holders.json")
    deadline=time.monotonic()+90
    while not (root/"release-child").exists():
        if time.monotonic()>deadline: raise RuntimeError("lock probe barrier timed out")
        time.sleep(.01)
os.execv(__file__+"-registry",[__file__+"-registry",*sys.argv[1:]])
`)
    const owner = spawn('zsh', ['-c', 'eval "$1"', 'fanout-lock-probe', c.commands[1]], { cwd: c.root, env: c.env, detached: true })
    const done = new Promise(resolveResult => owner.on('close', resolveResult))
    const gone = async pid => {
      const deadline = Date.now() + BARRIER_MS
      for (;;) {
        try { process.kill(pid, 0) } catch (e) { if (e.code === 'ESRCH') return; throw e }
        assert.ok(Date.now() < deadline, `pid ${pid} did not exit`)
        await new Promise(r => setTimeout(r, 20))
      }
    }
    let holders
    try {
      const deadline = Date.now() + BARRIER_MS
      while (!existsSync(join(c.root, 'holders.json'))) {
        assert.ok(Date.now() < deadline, 'lock probe readiness timed out')
        await new Promise(r => setTimeout(r, 10))
      }
      holders = JSON.parse(readFileSync(join(c.root, 'holders.json'), 'utf8'))
      const record = JSON.parse(readFileSync(join(c.args.stateDir, 'rq-test.lock'), 'utf8'))
      const cliPath = join(c.args.stateDir, 'rq-test.lock.cli')
      // The helper names its child right after the spawn; the child may start first.
      while (!existsSync(cliPath) || JSON.parse(readFileSync(cliPath, 'utf8')).pid !== holders.cli) {
        assert.ok(Date.now() < deadline, 'CLI holder record timed out')
        await new Promise(r => setTimeout(r, 10))
      }
      const cli = JSON.parse(readFileSync(cliPath, 'utf8'))
      assert.equal(record.helper.pid, holders.helper, 'the runner records its helper child')
      assert.equal(cli.pid, holders.cli, 'the helper records the CLI child that inherits the lock')
      process.kill(record.launcher.pid, 'SIGKILL'); process.kill(record.helper.pid, 'SIGKILL')
      await gone(record.launcher.pid); await gone(record.helper.pid)
      c.env.SUBFLEET_FANOUT_LOCK_WAIT_S = '.1'
      const expected = new RegExp(`^LOCKED lock=rq-test\\.lock wait_s=0\\.1 launcher_pid=${record.launcher.pid} launcher=dead helper_pid=${record.helper.pid} helper=dead cli_pid=${cli.pid} cli=alive$`, 'm')
      for (const i of [0, 1, 2]) {
        const blocked = c.step(i)
        assert.equal(blocked.status, 75, blocked.text)
        assert.match(blocked.text, expected)
        assert.doesNotMatch(blocked.text, /CHECKSUM_OK|SETUP_OK|SUBMIT_RC|JOBS=|WAIT_RC/)
      }
      writeFileSync(join(c.root, 'release-child'), '')
      await gone(cli.pid); await done
      delete c.env.SUBFLEET_FANOUT_LOCK_WAIT_S
      c.setup(); assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
      assert.equal(existsSync(join(c.args.stateDir, 'rq-test.lock.cli')), false, 'a finished CLI child is no longer named')
    } finally {
      writeFileSync(join(c.root, 'release-child'), '')
      try { process.kill(-owner.pid, 'SIGKILL') } catch (e) { if (e.code !== 'ESRCH') throw e }
      if (holders) for (const pid of [holders.cli, holders.helper]) {
        try { process.kill(pid, 'SIGKILL') } catch (e) { if (e.code !== 'ESRCH') throw e }
      }
    }
    // Liveness is per recorded spawn: a live pid that started after its
    // record is reported reused, and a round-7 bare pid is still named.
    const d = await make(); d.setup(); d.env.SUBFLEET_FANOUT_LOCK_WAIT_S = '0'
    for (const [record, line] of [
      [JSON.stringify({ launcher: { pid: process.pid, since: 1 }, helper: { pid: process.pid, since: Date.now() / 1000 } }),
        `launcher_pid=${process.pid} launcher=reused helper_pid=${process.pid} helper=alive cli_pid=none`],
      [String(process.pid), `launcher_pid=${process.pid} launcher=alive helper_pid=none cli_pid=none`],
      ['not a record', 'launcher_pid=none helper_pid=none cli_pid=none'],
    ]) {
      const holder = await holdLock(d, record)
      try {
        const blocked = d.step(1)
        assert.equal(blocked.status, 75, blocked.text)
        assert.match(blocked.text, new RegExp(`^LOCKED lock=rq-test\\.lock wait_s=0 ${line}$`, 'm'))
      } finally { holder.kill('SIGKILL') }
      await new Promise(r => holder.on('close', r))
    }
  })
  await test('round 8 finding 4: unavailable policy proof leaves only rewritten pins uncertain across relaunch', async () => {
    // Replaces round 6's check of the same name: unpinned jobs and pins the
    // daemon recorded as written are proven by the creation label alone.
    for (const mode of ['down', 'bad', 'missing']) {
      for (const [model, provable] of [[undefined, true], ['opus', true], ['sol', false]]) {
        const c = await make('rowless-unknown,check-prompts'); c.env.FAKE_POLICY_PROOF = mode
        if (model) c.args.model = model
        const rendered = await renderWorkflow(script, c.args)
        c.step(0, rendered.commands[0]); c.step(1, rendered.commands[1])
        const snapshots = JSON.parse(readFileSync(join(c.args.stateDir, 'rq-test.submit.1.jsonl.identity.json'), 'utf8'))
        assert.deepEqual(Object.keys(snapshots).sort(), ['a', 'b', 'c'], `${mode} ${model}: a snapshot is kept without the preview`)
        assert.equal(c.count('preview'), 1, `${mode} ${model}: one failed preview per model and invocation`)
        unlinkSync(c.args.items[1].promptPath)
        const refusal = c.step(1, rendered.commands[1]).text
        assert.equal(c.count('preview'), 2)
        if (provable) {
          assert.match(refusal, /MANIFEST_REFUSED[\s\S]*JOBS=3\nUNSETTLED=0/, `${mode} ${model}: ${refusal}`)
        } else {
          assert.match(refusal, /MANIFEST_REFUSED[\s\S]*JOBS=0\nUNSETTLED=3/, `${mode} ${model}: ${refusal}`)
          assert.equal((refusal.match(/^LOOKUP_UNKNOWN name=\w .*recorded model astra is not the model as written, sol; only the policy preview resolves an alias, and it failed: /gm) || []).length, 3, refusal)
          assert.doesNotMatch(refusal, /^SUBMITTED |^REFUSED /m)
        }
        delete c.env.FAKE_POLICY_PROOF
        c.step(0, rendered.commands[0])
        assert.match(c.step(1, rendered.commands[1]).text, provable ? /JOBS=3\nUNSETTLED=0/ : /JOBS=0\nUNSETTLED=3/)
      }
    }
  })
  await test('round 8 finding 4: a hung preview is asked once per invocation, and its snapshots still prove', async () => {
    const c = await make('rowless-unknown,check-prompts'); c.env.FAKE_POLICY_PROOF = 'hang'
    c.setup()
    const started = Date.now(), first = c.step(1).text, elapsed = Date.now() - started
    assert.equal(c.count('preview'), 1, first)
    assert.match(first, /JOBS=0\nUNSETTLED=3/)
    unlinkSync(c.args.items[1].promptPath)
    assert.match(c.step(1).text, /MANIFEST_REFUSED[\s\S]*JOBS=3\nUNSETTLED=0/)
    assert.equal(c.count('preview'), 2)
    console.log(`  hung preview: first step 1 took ${(elapsed / 1000).toFixed(1)} s with one 15 s preview timeout`)
  })
  await test('round 8 finding 4: LOOKUP_UNKNOWN says when the creating snapshot is missing', async () => {
    const c = await make('rowless-unknown,check-prompts'); c.setup(); c.step(1)
    unlinkSync(join(c.args.stateDir, 'rq-test.submit.1.jsonl.identity.json'))
    unlinkSync(c.args.items[1].promptPath)
    const refusal = c.step(1).text
    assert.match(refusal, /MANIFEST_REFUSED[\s\S]*JOBS=0\nUNSETTLED=3/)
    assert.equal((refusal.match(/^LOOKUP_UNKNOWN name=\w .*snapshot missing for the creating invocation rq-test\.submit\.1\.jsonl: the invocation's snapshot file is missing or unreadable$/gm) || []).length, 3, refusal)
  })
  await test('round 8 nit: an invalid lock wait setting falls back to the default', async () => {
    for (const value of ['abc', 'nan', '-1', '']) {
      const c = await make(); c.env.SUBFLEET_FANOUT_LOCK_WAIT_S = value
      const r = c.step(0)
      assert.equal(r.status, 0, `${value}: ${r.text}`)
      assert.match(r.text, /SETUP_OK/)
      assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
    }
  })
  await test('round 8 nit: ids an older helper recovered say why they are unknown now', async () => {
    const c = await make('rowless-unknown,check-prompts'); c.setup(); c.step(1)
    unlinkSync(c.args.items[1].promptPath)
    assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
    // The 8d074a715 helper's snapshots and recovery rows carry no creation mark.
    const strip = path => {
      const lines = readFileSync(path, 'utf8').trim().split('\n').map(JSON.parse)
      writeFileSync(path, lines.map(row => { delete (row.identity || {}).submit_batch; return JSON.stringify(row) }).join('\n') + '\n')
    }
    strip(join(c.args.stateDir, 'rq-test.recovered.jsonl'))
    const identityPath = join(c.args.stateDir, 'rq-test.submit.1.jsonl.identity.json')
    const identities = JSON.parse(readFileSync(identityPath, 'utf8'))
    for (const name of ['a', 'b', 'c']) delete identities[name].submit_batch
    writeFileSync(identityPath, JSON.stringify(identities))
    c.setup()
    const relaunched = c.step(1).text
    assert.match(relaunched, /JOBS=0\nUNSETTLED=3/)
    assert.equal((relaunched.match(/^OUTCOME_UNKNOWN name=\w .*an older helper recovered job job-\w here without a creation mark.*install between batches\)$/gm) || []).length, 3, relaunched)
  })
  await test('round 8 nit: the round-7 report section cites main, release/217 and the installed build', () => {
    const report = readFileSync(join(harness, '../../2026-09-22-fanout-workflow.md'), 'utf8')
    const round7 = report.slice(report.indexOf('## Round 7'), report.indexOf('## Round 8'))
    assert.ok(round7.length > 1000)
    assert.doesNotMatch(round7, /eef8d3d9b/)
    for (const commit of ['c230d1d8e', 'f3e042953', 'c6975725']) assert.match(round7, new RegExp(commit))
  })
  for (const [field, variable] of [['workdir_head', 'FAKE_SUBMIT_HEAD'], ['policy_hash', 'FAKE_SUBMIT_POLICY']]) {
    for (const lost of ['lose-stdout', 'unknown-c-after-old']) {
      await test(`round 7 finding 1: ${field} changes at submit (${lost})`, async () => {
        const c = await make('check-prompts')
        const old = await renderWorkflow(script, { ...c.args, stateDir: join(c.root, 'old-state') })
        c.step(0, old.commands[0]); assert.match(c.step(1, old.commands[1]).text, /JOBS=3\nUNSETTLED=0/)
        const before = readFileSync(join(c.root, 'registry.json'), 'utf8')
        c.env.FAKE_CAPTURE_PAYLOADS = join(c.root, 'actual-payloads.jsonl')
        c.env[variable] = 'different-at-submit'; c.env.SCENARIO = `${lost},check-prompts`
        c.setup()
        const uncertain = lost === 'lose-stdout' ? 3 : 1
        assert.match(c.step(1).text, new RegExp(`JOBS=0\nUNSETTLED=${uncertain}`))
        c.env[field === 'workdir_head' ? 'FAKE_WORKDIR_HEAD' : 'FAKE_POLICY_HASH'] = 'different-at-submit'
        c.env.SCENARIO = 'check-prompts'
        const conflict = c.step(1).text
        assert.match(conflict, new RegExp(`LOOKUP_UNKNOWN[\\s\\S]*JOBS=0\nUNSETTLED=${uncertain}`))
        assert.doesNotMatch(conflict, /^SUBMITTED /m)
        c.setup(); assert.match(c.step(1).text, new RegExp(`JOBS=0\nUNSETTLED=${uncertain}`))
        assert.match(c.step(2).text, /NOTHING_TO_WAIT/)
        assert.equal(readFileSync(join(c.root, 'registry.json'), 'utf8'), before)
        const registry = JSON.parse(before)
        const actual = readFileSync(c.env.FAKE_CAPTURE_PAYLOADS, 'utf8').trim().split('\n').map(JSON.parse)
        assert.equal(actual.length, 9) // Lost attempt, conflict, then relaunch.
        assert.ok(actual.every(row => row[field] === 'different-at-submit' && row.payload_digest !== registry[row.request_id].payload_digest))
      })
    }
  }
  for (const [alias, canonical] of [['sol', 'astra'], ['fable', 'opus']]) {
    await test(`round 7 finding 2: ${alias} alias recovers after lost answers and validation refusal`, async () => {
      const c = await make('rowless-unknown,check-prompts')
      c.args.model = alias
      const rendered = await renderWorkflow(script, c.args)
      c.step(0, rendered.commands[0]); assert.match(c.step(1, rendered.commands[1]).text, /JOBS=0\nUNSETTLED=3/)
      unlinkSync(c.args.items[1].promptPath)
      assert.match(c.step(1, rendered.commands[1]).text, /MANIFEST_REFUSED[\s\S]*JOBS=3\nUNSETTLED=0/)
      c.step(0, rendered.commands[0]); assert.match(c.step(1, rendered.commands[1]).text, /JOBS=3\nUNSETTLED=0/)
      const registry = JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))
      const snapshots = JSON.parse(readFileSync(join(c.args.stateDir, 'rq-test.submit.1.jsonl.identity.json'), 'utf8'))
      for (const name of ['a', 'b', 'c']) {
        assert.equal(snapshots[name].pinned_model, canonical)
        assert.equal(registry[`rq-test-${['a', 'b', 'c'].indexOf(name) + 1}`].pinned_model, canonical)
        assert.equal(snapshots[name].payload_digest, registry[`rq-test-${['a', 'b', 'c'].indexOf(name) + 1}`].payload_digest)
      }
      assert.equal(c.count('run'), 2)
      assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
    })
  }
  await test('round 7: recorded HEAD and policy prove a job created after snapshot capture', async () => {
    for (const variable of ['FAKE_SUBMIT_HEAD', 'FAKE_SUBMIT_POLICY']) {
      const c = await make('rowless-unknown,check-prompts')
      c.env[variable] = 'different-at-submit'
      c.setup(); assert.match(c.step(1).text, /JOBS=0\nUNSETTLED=3/)
      unlinkSync(c.args.items[1].promptPath)
      assert.match(c.step(1).text, /MANIFEST_REFUSED[\s\S]*JOBS=3\nUNSETTLED=0/)
      c.setup(); assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
      const field = variable === 'FAKE_SUBMIT_HEAD' ? 'workdir_head' : 'policy_hash'
      const proof = JSON.parse(readFileSync(join(c.args.stateDir, 'rq-test.recovered.jsonl'), 'utf8').split('\n')[0])
      assert.equal(proof[field], 'different-at-submit')
      assert.notEqual(proof.identity[field], proof[field])
      assert.deepEqual(proof.submit_batch, proof.identity.submit_batch)
    }
  })
  await test('round 7: missing or different creation markers cannot settle old requests', async () => {
    for (const mode of ['MISSING', 'MISMATCH']) {
      const c = await make('rowless-unknown,check-prompts'); c.setup(); c.step(1)
      unlinkSync(c.args.items[1].promptPath); c.env[`FAKE_SHOW_${mode}`] = 'batch'
      assert.match(c.step(1).text, /LOOKUP_UNKNOWN[\s\S]*JOBS=0\nUNSETTLED=3/)
      delete c.env[`FAKE_SHOW_${mode}`]
      c.setup(); assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
    }
  })
  await test('round 7: a surviving fake CLI holds the lock; bounded wait reports its launcher', async () => {
    const c = await make('check-prompts'); c.setup()
    const realPython = execFileSync('python3', ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8' }).trim()
    const fake = join(c.root, 'bin', 'subfleet'); copyFileSync(fake, fake + '-registry')
    writeFileSync(fake, `#!${realPython}
import json,os,pathlib,sys,time
root=pathlib.Path(os.environ["FAKE_STATE"])
if sys.argv[1] == "run" and "--batch" in sys.argv:
    (root/"holders.new").write_text(json.dumps({"cli":os.getpid(),"helper":os.getppid()}))
    os.replace(root/"holders.new",root/"holders.json")
    deadline=time.monotonic()+90
    while not (root/"release-child").exists():
        if time.monotonic()>deadline: raise RuntimeError("lock probe barrier timed out")
        time.sleep(.01)
os.execv(__file__+"-registry",[__file__+"-registry",*sys.argv[1:]])
`)
    const owner = spawn('zsh', ['-c', 'eval "$1"', 'fanout-lock-probe', c.commands[1]], { cwd: c.root, env: c.env, detached: true })
    const done = new Promise(resolveResult => owner.on('close', resolveResult))
    let holders
    try {
      const deadline = Date.now() + BARRIER_MS
      while (!existsSync(join(c.root, 'holders.json'))) {
        assert.ok(Date.now() < deadline, 'lock probe readiness timed out')
        await new Promise(r => setTimeout(r, 10))
      }
      holders = JSON.parse(readFileSync(join(c.root, 'holders.json'), 'utf8'))
      // Round 7 wrote a bare launcher pid; round 8 writes JSON naming holders.
      const record = JSON.parse(readFileSync(join(c.args.stateDir, 'rq-test.lock'), 'utf8'))
      const launcher = typeof record === 'number' ? record : record.launcher.pid
      process.kill(owner.pid, 'SIGKILL'); process.kill(holders.helper, 'SIGKILL')
      c.env.SUBFLEET_FANOUT_LOCK_WAIT_S = '.1'
      const start = Date.now(), blocked = c.step(0)
      assert.equal(blocked.status, 75, blocked.text)
      assert.match(blocked.text, new RegExp(`^LOCKED lock=rq-test\\.lock .*launcher_pid=${launcher}\\b.*`, 'm'))
      assert.match(blocked.text, /wait_s=0\.1\b/)
      assert.ok(Date.now() - start < 15000)
      assert.doesNotMatch(blocked.text, /CHECKSUM_OK|SETUP_OK/)
      writeFileSync(join(c.root, 'release-child'), '')
      await done
      delete c.env.SUBFLEET_FANOUT_LOCK_WAIT_S
      c.setup(); assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
    } finally {
      writeFileSync(join(c.root, 'release-child'), '')
      try { process.kill(-owner.pid, 'SIGKILL') } catch (e) { if (e.code !== 'ESRCH') throw e }
      if (holders) for (const pid of [holders.cli, holders.helper]) {
        try { process.kill(pid, 'SIGKILL') } catch (e) { if (e.code !== 'ESRCH') throw e }
      }
    }
  })
  for (const field of ['workdir_head', 'policy_hash']) {
    await test(`round 6 finding 2: reused request id with changed ${field} stays uncertain`, async () => {
      const c = await make('check-prompts')
      const old = await renderWorkflow(script, { ...c.args, stateDir: join(c.root, 'old-state') })
      c.step(0, old.commands[0]); c.step(1, old.commands[1])
      c.env[field === 'workdir_head' ? 'FAKE_WORKDIR_HEAD' : 'FAKE_POLICY_HASH'] = 'different'
      c.env.SCENARIO = 'lose-stdout,check-prompts'; c.setup()
      assert.match(c.step(1).text, /JOBS=0\nUNSETTLED=3/)
      const registryBefore = readFileSync(join(c.root, 'registry.json'), 'utf8')
      c.env.SCENARIO = 'check-prompts'
      const conflict = c.step(1).text
      assert.match(conflict, /LOOKUP_UNKNOWN[\s\S]*JOBS=0\nUNSETTLED=3/)
      assert.doesNotMatch(conflict, /^SUBMITTED |^REFUSED /m)
      c.setup()
      assert.match(c.step(1).text, /JOBS=0\nUNSETTLED=3/)
      assert.match(c.step(2).text, /NOTHING_TO_WAIT/)
      assert.equal(readFileSync(join(c.root, 'registry.json'), 'utf8'), registryBefore)
    })
  }
  await test('round 6 finding 3: prompt edit after snapshot submits the snapshot bytes', async () => {
    const c = await make('check-prompts')
    // A lost creation belongs to this invocation. An older job from another
    // state cannot prove that a later lost dedupe attempt used its HEAD/policy.
    const fake = join(c.root, 'bin', 'subfleet')
    copyFileSync(fake, fake + '-registry')
    writeFileSync(fake, '#!/bin/bash\nif [ "$1" = run ] && [[ " $* " != *" --dry-run "* ]] && [ -n "${FAKE_EDIT_PROMPT:-}" ]; then printf "Different prompt after snapshot" > "$FAKE_EDIT_PROMPT"; fi\nexec "$(dirname "$0")/subfleet-registry" "$@"\n')
    c.env.FAKE_EDIT_PROMPT = c.args.items[1].promptPath
    c.env.FAKE_CAPTURE_ATTEMPT = join(c.root, 'attempt.json')
    c.env.SCENARIO = 'lose-stdout,check-prompts'; c.setup(); c.step(1)
    const identity = JSON.parse(readFileSync(join(c.args.stateDir, 'rq-test.submit.1.jsonl.identity.json'), 'utf8'))
    const attempted = JSON.parse(readFileSync(c.env.FAKE_CAPTURE_ATTEMPT, 'utf8'))
    delete c.env.FAKE_EDIT_PROMPT; c.env.SCENARIO = 'check-prompts'
    const recovered = c.step(1).text
    c.setup(); const relaunched = c.step(1).text
    assert.equal(identity.b.prompt_sha256, attempted.b, 'snapshot must describe the bytes actually submitted; recovery and relaunch:\n' + recovered + relaunched)
    const staged = JSON.parse(readFileSync(join(c.args.stateDir, 'rq-test.submit.1.jsonl.manifest.json'), 'utf8'))
    assert.notEqual(staged.jobs[1].prompt, c.args.items[1].promptPath)
    assert.equal(readFileSync(staged.jobs[1].prompt, 'utf8'), 'Review b')
    assert.equal(readFileSync(c.args.items[1].promptPath, 'utf8'), 'Different prompt after snapshot')
    const registry = JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))
    assert.equal(identity.b.payload_digest, registry['rq-test-2'].payload_digest, JSON.stringify({ identity: identity.b, recorded: registry['rq-test-2'] }))
    assert.match(recovered, /SUBMITTED name=b job_id=job-b created=False[\s\S]*JOBS=3\nUNSETTLED=0/)
    assert.match(relaunched, /JOBS=3\nUNSETTLED=0/)
  })
  await test('round 6 finding 1: synchronized submissions preserve successful evidence', async () => {
    for (const killOwner of [false, true]) {
      const c = await make('check-prompts'); c.setup()
      const realPython = execFileSync('python3', ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8' }).trim()
      const wrapper = join(c.root, 'bin', 'synchronized-python')
      writeFileSync(wrapper, `#!${realPython}
import os,pathlib,subprocess,sys,time
real=${JSON.stringify(realPython)}
root=pathlib.Path(os.environ["FAKE_STATE"]); role=os.environ.get("FAKE_ROLE", "")
verb=next((arg for arg in sys.argv[1:] if arg in ("should-submit", "submit", "next-log")), "")
if role and "-c" in sys.argv: (root/("lock-owner-"+role)).write_text(str(os.getpid()))
def wait(path):
    deadline=time.monotonic()+20
    while not path.exists():
        if time.monotonic()>deadline: raise RuntimeError("barrier timed out: "+str(path))
        time.sleep(.01)
if role and verb in ("should-submit", "submit"): (root/("entry-"+role)).touch()
if role and verb == "next-log":
    result=subprocess.run([real,*sys.argv[1:]],capture_output=True,text=True)
    (root/("next-ready-"+role)).write_text(result.stdout)
    wait(root/("next-ready-refuser" if role == "creator" else "creator-done"))
    sys.stdout.write(result.stdout); sys.exit(result.returncode)
os.execv(real,[real,*sys.argv[1:]])
`)
      chmodSync(wrapper, 0o755)
      const fake = join(c.root, 'bin', 'subfleet')
      copyFileSync(fake, fake + '-registry')
      writeFileSync(fake, `#!${realPython}
import os,pathlib,subprocess,sys,time
root=pathlib.Path(os.environ["FAKE_STATE"])
role=os.environ.get("FAKE_ROLE", "")
env=dict(os.environ)
if sys.argv[1] == "run" and "--dry-run" not in sys.argv:
    if role == "creator":
        (root/"creator-run-ready").touch()
        deadline=time.monotonic()+90
        while not (root/"release-creator").exists():
            if time.monotonic()>deadline: raise RuntimeError("creator barrier timed out")
            time.sleep(.01)
    elif role == "refuser":
        (root/"refuser-run-ready").touch()
        env["SCENARIO"]="manifest-refusal"
result=subprocess.run([str(root/"bin/subfleet-registry"),*sys.argv[1:]],env=env)
if role == "creator" and sys.argv[1] == "run" and "--dry-run" not in sys.argv: (root/"creator-done").touch()
sys.exit(result.returncode)
`)
      const command = c.commands[1].replaceAll('python3', `'${wrapper}'`)
      const launch = role => new Promise((resolveResult, reject) => {
        const child = spawn('zsh', ['-c', 'eval "$1"', 'fanout-overlap', command], { cwd: c.root, env: { ...c.env, FAKE_ROLE: role }, timeout: STEP_TIMEOUT_MS })
        let text = ''; child.stdout.on('data', b => { text += b }); child.stderr.on('data', b => { text += b })
        child.on('error', reject); child.on('close', status => resolveResult({ status, text }))
      })
      const until = async predicate => {
        const deadline = Date.now() + BARRIER_MS
        while (!predicate()) { assert.ok(Date.now() < deadline, 'overlap barrier timed out'); await new Promise(r => setTimeout(r, 10)) }
      }
      const creator = launch('creator')
      await until(() => existsSync(join(c.root, 'next-ready-creator')) || existsSync(join(c.root, 'creator-run-ready')))
      const refuser = launch('refuser')
      await until(() => existsSync(join(c.root, 'entry-refuser')))
      if (killOwner) {
        process.kill(Number(readFileSync(join(c.root, 'lock-owner-creator'), 'utf8')), 'SIGKILL')
        await new Promise(r => setTimeout(r, 100))
        assert.equal(existsSync(join(c.root, 'refuser-run-ready')), false, 'surviving child must keep the lock')
      }
      writeFileSync(join(c.root, 'release-creator'), '')
      const results = await Promise.all([creator, refuser])
      if (!killOwner) assert.equal(results[0].status, 0, results[0].text)
      if (results[1].status === 75) {
        // Under load the creator can hold the lock past the 5 s budget. The
        // refuser then stops having written nothing; the relaunch below checks.
        assert.match(results[1].text, /^LOCKED lock=rq-test\.lock /m)
        assert.doesNotMatch(results[1].text, /SUBMIT_RC|JOBS=/)
        lockedPeers++
      } else assert.equal(results[1].status, 0, results[1].text)
      assert.equal(Object.keys(JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))).length, 3)
      c.setup()
      const relaunched = c.step(1).text
      assert.match(relaunched, /JOBS=3\nUNSETTLED=0/)
      assert.doesNotMatch(relaunched, /^REFUSED /m)
      assert.equal(c.count('run'), 1)
      assert.match(c.wait(), /DONE name=b job_id=job-b state=succeeded/)
    }
  })
  await test('randomized sequential replay: 20,000 histories', async () => {
    const c = await make(); c.setup()
    const result = spawnSync('python3', [join(harness, 'replay.py'), join(c.args.stateDir, 'rq-test.fanout.py')], { env: c.env, encoding: 'utf8', timeout: STEP_TIMEOUT_MS })
    assert.equal(result.status, 0, result.stdout + result.stderr)
    console.log(result.stdout.trim())
  })
  await test('randomized concurrent replay: 64 histories with two overlapping invocations', async () => {
    const c = await make(); c.setup()
    const command = join(c.root, 'step1.sh'); writeFileSync(command, c.commands[1])
    const result = await new Promise((resolveResult, reject) => {
      const child = spawn('python3', [join(harness, 'replay.py'), join(c.args.stateDir, 'rq-test.fanout.py'), '--concurrent', command], { env: c.env })
      let output = ''
      child.stdout.on('data', b => { output += b; process.stdout.write(b) })
      child.stderr.on('data', b => { output += b })
      child.on('error', reject); child.on('close', status => resolveResult({ status, output }))
    })
    assert.equal(result.status, 0, result.output)
  })
  await test('round 6: canonical snapshots agree with the daemon payload digest', async () => {
    const c = await make(); c.setup(); c.step(1)
    const result = spawnSync('python3', ['-c', `import json,sys
from pathlib import Path
from subfleet.ids import payload_digest
path=Path(sys.argv[1])
identities=json.loads(Path(str(path)+".identity.json").read_text())
manifest=json.loads(Path(str(path)+".manifest.json").read_text())
for job in manifest["jobs"]:
    identity=identities[job["name"]]
    actual=payload_digest(Path(job["prompt"]).read_bytes(), workdir=job["workdir"],
        workdir_head=identity["workdir_head"], task=job["task"], tier=job["tier"],
        pinned_model=job.get("model"), sandbox=job.get("sandbox", "read-only"),
        out_path=job["out"], policy_hash=identity["policy_hash"])
    assert actual == identity["payload_digest"], (actual, identity)
`, join(c.args.stateDir, 'rq-test.submit.1.jsonl')], { env: c.env, encoding: 'utf8' })
    assert.equal(result.status, 0, result.stdout + result.stderr)
  })
  await test('round 6: recovery retains the original commit and policy after both move', async () => {
    const c = await make('unknown-c'); c.setup(); c.step(1)
    c.env.FAKE_WORKDIR_HEAD = 'head-two'; c.env.FAKE_POLICY_HASH = 'policy-two'
    const conflict = c.step(1).text
    assert.match(conflict, /SUBMITTED name=c job_id=job-c created=False[\s\S]*JOBS=3\nUNSETTLED=0/)
    assert.equal(c.count('show'), 1)
    c.setup(); assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
    assert.equal(c.count('run'), 2)
    assert.equal(c.count('show'), 1)
  })
  await test('round 5 finding 1: earlier refusal becomes uncertain after a later lost answer', async () => {
    const c = await make('refuse-b,unknown-c'); c.setup()
    assert.match(c.step(1).text, /REFUSED name=b[\s\S]*OUTCOME_UNKNOWN name=c/)
    c.env.SCENARIO = 'lose-stdout,check-prompts'
    const second = c.step(1).text
    const registry = JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))
    assert.equal(registry['rq-test-2'].job_id, 'job-b')
    unlinkSync(c.args.items[1].promptPath)
    c.env.SCENARIO = 'check-prompts,no-name'
    const third = c.step(1).text
    assert.match(third, /MANIFEST_REFUSED[\s\S]*SUBMITTED name=b job_id=job-b created=False/)
    assert.match(second, /OUTCOME_UNKNOWN name=b request_id=rq-test-2/)
    assert.match(third, /JOBS=3\nUNSETTLED=0/)
    assert.doesNotMatch(third, /REFUSED name=b|RETRY_STEP1/)
    assert.equal(c.count('run'), 3)
    assert.match(c.wait(), /DONE name=b job_id=job-b state=succeeded/)
    c.setup(); c.step(1)
    assert.equal(c.count('run'), 3)
    assert.equal(Object.keys(JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))).length, 3)
  })
  await test('round 5 finding 2: reused request ids cannot recover a different manifest', async () => {
    const c = await make('check-prompts')
    const old = await renderWorkflow(script, { ...c.args, stateDir: join(c.root, 'old-state'),
      items: c.args.items.map(j => ({ ...j, name: `old-${j.name}`, outPath: join(c.root, `old-${j.name}.md`) })) })
    assert.equal(c.step(0, old.commands[0]).status, 0)
    assert.match(c.step(1, old.commands[1]).text, /JOBS=3\nUNSETTLED=0/)
    c.env.SCENARIO = 'lose-stdout,check-prompts'
    c.setup()
    assert.match(c.step(1).text, /JOBS=0\nUNSETTLED=3/)
    const registryBefore = readFileSync(join(c.root, 'registry.json'), 'utf8')
    assert.equal(JSON.parse(registryBefore)['rq-test-2'].job_id, 'job-old-b')
    unlinkSync(c.args.items[1].promptPath)
    c.env.SCENARIO = 'check-prompts'
    const refusal = c.step(1).text
    assert.match(refusal, /MANIFEST_REFUSED[\s\S]*JOBS=0\nUNSETTLED=3/)
    assert.equal((refusal.match(/^OUTCOME_UNKNOWN name=/gm) || []).length, 3)
    assert.doesNotMatch(refusal, /^SUBMITTED |^REFUSED |RETRY_STEP1/m)
    assert.match(c.step(2).text, /NOTHING_TO_WAIT/)
    c.setup(); c.step(1)
    assert.equal(c.count('run'), 3)
    assert.equal(c.count('wait'), 0)
    assert.equal(readFileSync(join(c.root, 'registry.json'), 'utf8'), registryBefore)
  })
  await test('round 5: each unanswered retry captures its own prompt identity', async () => {
    const c = await make('refuse-b,unknown-c'); c.setup(); c.step(1)
    writeFileSync(c.args.items[1].promptPath, 'New prompt for the later invocation')
    c.env.SCENARIO = 'lose-stdout,check-prompts'; c.step(1)
    unlinkSync(c.args.items[1].promptPath)
    c.env.SCENARIO = 'check-prompts'
    assert.match(c.step(1).text, /SUBMITTED name=b job_id=job-b created=False[\s\S]*JOBS=3\nUNSETTLED=0/)
    assert.match(c.wait(), /DONE name=b job_id=job-b state=succeeded/)
  })
  await test('round 5: same paths and names with different prompt bytes cannot recover old jobs', async () => {
    const c = await make('check-prompts')
    const old = await renderWorkflow(script, { ...c.args, stateDir: join(c.root, 'old-state') })
    c.step(0, old.commands[0]); c.step(1, old.commands[1])
    for (const item of c.args.items) writeFileSync(item.promptPath, `Different prompt for ${item.name}`)
    c.env.SCENARIO = 'lose-stdout,check-prompts'; c.setup(); c.step(1)
    // A C-6.2 conflict names the old job but does not prove it belongs here.
    c.env.SCENARIO = 'check-prompts'
    const conflict = c.step(1).text
    assert.match(conflict, /LOOKUP_UNKNOWN[\s\S]*JOBS=0\nUNSETTLED=3/)
    assert.doesNotMatch(conflict, /^SUBMITTED |^REFUSED /m)
    unlinkSync(c.args.items[1].promptPath)
    assert.match(c.step(1).text, /MANIFEST_REFUSED[\s\S]*JOBS=0\nUNSETTLED=3/)
    assert.equal(c.count('wait'), 0)
    assert.equal(Object.keys(JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))).length, 3)
  })
  await test('round 5: C-6.2 conflict after a prompt edit verifies the original unknown job', async () => {
    const c = await make('unknown-c'); c.setup(); c.step(1)
    writeFileSync(c.args.items[2].promptPath, 'Edited after the answer was lost')
    c.env.SCENARIO = ''
    const conflict = c.step(1).text
    assert.match(conflict, /SUBMITTED name=c job_id=job-c created=False[\s\S]*JOBS=3\nUNSETTLED=0/)
    assert.equal(c.count('show'), 1)
    assert.equal(c.count('runs'), 0)
    c.setup(); c.step(1)
    assert.equal(c.count('run'), 2)
    assert.equal(c.count('show'), 1)
    assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
  })
  for (const mode of ['MISSING', 'MISMATCH']) {
    await test(`round 5: ${mode.toLowerCase()} recorded identity fields keep jobs uncertain`, async () => {
      for (const field of ['job_id', 'request_id', 'name', 'workdir', 'out_path', 'task', 'tier', 'sandbox', 'pinned_model', 'prompt_path', 'workdir_head', 'policy_hash', 'payload_digest']) {
        const c = await make('rowless-unknown,check-prompts'); c.setup(); c.step(1)
        unlinkSync(c.args.items[1].promptPath)
        c.env[`FAKE_SHOW_${mode}`] = field
        const refusal = c.step(1).text
        assert.match(refusal, /MANIFEST_REFUSED[\s\S]*JOBS=0\nUNSETTLED=3/, `${mode} ${field}: ${refusal}`)
        assert.equal((refusal.match(/^LOOKUP_UNKNOWN name=/gm) || []).length, 3)
        assert.doesNotMatch(refusal, /^SUBMITTED |^REFUSED |RETRY_STEP1/m)
        assert.match(c.step(2).text, /NOTHING_TO_WAIT/)
        delete c.env[`FAKE_SHOW_${mode}`]
        c.setup()
        assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
        assert.equal(c.count('run'), 2)
      }
    })
  }
  await test('round 5: flat offline show metadata is verified without acknowledging caller notices', async () => {
    const c = await make('rowless-unknown,check-prompts'); c.setup(); c.step(1)
    unlinkSync(c.args.items[1].promptPath)
    c.env.FAKE_SHOW_FLAT = '1'
    assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
    assert.equal(c.count('show'), 3)
    assert.equal(c.env.CLAUDE_CODE_SESSION_ID, 'fanout-harness-caller')
    c.setup(); c.step(1)
    assert.equal(c.count('show'), 3)
    assert.equal(c.count('run'), 2)
  })
  await test('round 5: legacy recovery records without invocation identity cannot settle jobs', async () => {
    const c = await make('rowless-unknown,check-prompts'); c.setup(); c.step(1)
    unlinkSync(c.args.items[1].promptPath)
    const registry = JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))
    writeFileSync(join(c.args.stateDir, 'rq-test.recovered.jsonl'), Object.values(registry).map(row => JSON.stringify(row)).join('\n') + '\n')
    const identityPath = join(c.args.stateDir, 'rq-test.submit.1.jsonl.identity.json')
    if (existsSync(identityPath)) unlinkSync(identityPath)
    const refusal = c.step(1).text
    assert.match(refusal, /MANIFEST_REFUSED[\s\S]*JOBS=0\nUNSETTLED=3/)
    assert.doesNotMatch(refusal, /^SUBMITTED |^REFUSED /m)
    c.setup()
    assert.match(c.step(1).text, /JOBS=0\nUNSETTLED=3/)
    assert.equal(c.count('run'), 2)
  })
  await test('round 4 finding 1: unknown survives not-sent before digest-conflict recovery', async () => {
    const c = await make('unknown-c'); c.setup()
    assert.match(c.step(1).text, /OUTCOME_UNKNOWN name=c/)
    const registry = JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))
    assert.equal(registry['rq-test-3'].job_id, 'job-c')
    c.env.SCENARIO = 'submit-down-always'
    const second = c.step(1).text
    c.env.SCENARIO = 'digest-conflict-c,no-name'
    const third = c.step(1).text
    assert.match(third, /SUBMITTED name=c job_id=job-c created=False/)
    assert.match(second, /OUTCOME_UNKNOWN name=c request_id=rq-test-3/)
    assert.doesNotMatch(third, /REFUSED name=c/)
    assert.match(third, /JOBS=3\nUNSETTLED=0/)
    assert.equal(c.count('run'), 3)
    assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
    c.setup(); c.step(1)
    assert.equal(c.count('run'), 3)
    assert.equal(Object.keys(JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))).length, 3)
  })
  await test('round 4 finding 2: validation refusal recovers earlier committed jobs by request id', async () => {
    const c = await make('rowless-unknown,check-prompts'); c.setup()
    assert.match(c.step(1).text, /JOBS=0\nUNSETTLED=3/)
    assert.equal(Object.keys(JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))).length, 3)
    unlinkSync(c.args.items[1].promptPath)
    const second = c.step(1).text
    assert.match(second, /MANIFEST_REFUSED[\s\S]*JOBS=3\nUNSETTLED=0/)
    assert.doesNotMatch(second, /^REFUSED name=|RETRY_STEP1/m)
    assert.equal(c.count('run'), 2)
    assert.equal(c.count('runs'), 3)
    assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
    writeFileSync(c.args.items[1].promptPath, 'Restored prompt')
    c.setup()
    assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
    assert.equal(c.count('run'), 2)
    assert.equal(c.count('runs'), 3)
    assert.equal(Object.keys(JSON.parse(readFileSync(join(c.root, 'registry.json'), 'utf8'))).length, 3)
  })
  await test('round 4: failed, empty and malformed lookups preserve uncertainty until relaunch', async () => {
    for (const lookup of ['lookup-down-always', 'lookup-empty', 'lookup-bad-json']) {
      const c = await make(`rowless-unknown,check-prompts,${lookup}`); c.setup(); c.step(1)
      unlinkSync(c.args.items[1].promptPath)
      const refusal = c.step(1).text
      assert.match(refusal, /MANIFEST_REFUSED[\s\S]*JOBS=0\nUNSETTLED=3/)
      assert.equal((refusal.match(/^OUTCOME_UNKNOWN name=/gm) || []).length, 3)
      assert.doesNotMatch(refusal, /^REFUSED name=|RETRY_STEP1/m)
      assert.equal(c.count('runs'), 3)
      // Round-3 refusal files had no log pointer. Upgrade must replay their
      // earlier unknown attempts, rather than trusting the old terminal state.
      const refusalPath = join(c.args.stateDir, 'rq-test.submit.refusal.json')
      const saved = JSON.parse(readFileSync(refusalPath, 'utf8'))
      delete saved.log
      writeFileSync(refusalPath, JSON.stringify(saved))
      c.env.SCENARIO = 'check-prompts'
      writeFileSync(c.args.items[1].promptPath, 'Restored prompt')
      c.setup()
      assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
      assert.equal(c.count('run'), 2)
      assert.equal(c.count('runs'), 6)
      assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
    }
  })
  await test('round 4: refusal after unknown and not-sent recovers only the missing id', async () => {
    const c = await make('unknown-c,check-prompts'); c.setup(); c.step(1)
    c.env.SCENARIO = 'submit-down-always,check-prompts'; c.step(1)
    c.env.SCENARIO = 'check-prompts'
    unlinkSync(c.args.items[1].promptPath)
    const third = c.step(1).text
    assert.match(third, /MANIFEST_REFUSED[\s\S]*SUBMITTED name=c job_id=job-c created=False/)
    assert.match(third, /JOBS=3\nUNSETTLED=0/)
    assert.doesNotMatch(third, /^REFUSED name=|RETRY_STEP1/m)
    assert.equal(c.count('run'), 3)
    assert.equal(c.count('runs'), 1)
    assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
  })
  await test('round 4: validation refusal after only not-sent attempts stays terminal', async () => {
    const c = await make('submit-down-always,check-prompts'); c.setup(); c.step(1)
    assert.equal(existsSync(join(c.root, 'registry.json')), false)
    c.env.SCENARIO = 'check-prompts'
    unlinkSync(c.args.items[1].promptPath)
    assert.match(c.step(1).text, /MANIFEST_REFUSED[\s\S]*JOBS=0\nUNSETTLED=0/)
    assert.equal(c.count('runs'), 0)
    c.setup(); c.step(1)
    assert.equal(c.count('run'), 2)
  })
  await test('finding 1: submit-down with request id retries from the first printed marker', async () => {
    const c = await make('submit-down'); c.setup()
    const outputs = c.submit()
    assert.match(outputs[0], /^RETRY_STEP1$/m)
    assert.equal(outputs.length, 2)
    assert.equal(c.count('run'), 2)
    assert.match(outputs[1], /JOBS=3\nUNSETTLED=0/)
    assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
  })
  await test('finding 1: unknown-c retries within the same run', async () => {
    const c = await make('unknown-c'); c.setup()
    const outputs = c.submit()
    assert.match(outputs[0], /OUTCOME_UNKNOWN name=c/)
    assert.equal(outputs.length, 2)
    assert.match(outputs[1], /RESUBMIT names=c/)
    assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
  })
  await test('finding 1: persistent submit-down stops after three calls', async () => {
    const c = await make('submit-down-always'); c.setup()
    const outputs = c.submit()
    assert.equal(outputs.length, 3)
    assert.equal(c.count('run'), 3)
    assert.match(outputs.at(-1), /JOBS=0\nUNSETTLED=3/)
  })
  await test('finding 2: whole-manifest refusal is terminal across step calls and relaunch', async () => {
    for (const scenario of ['manifest-refusal', 'manifest-refusal-marker']) {
      const c = await make(scenario); c.setup()
      const outputs = c.submit()
      assert.equal(outputs.length, 1)
      assert.match(outputs[0], /^MANIFEST_REFUSED rc=2 error=.*nothing was submitted/m)
      assert.equal((outputs[0].match(/^REFUSED name=/gm) || []).length, 3)
      assert.doesNotMatch(outputs[0], /OUTCOME_UNKNOWN|RETRY_STEP1/)
      assert.match(outputs[0], /UNSETTLED=0/)
      c.step(1); c.setup(); c.step(1)
      assert.equal(c.count('run'), 1)
      assert.equal(c.count('wait'), 0)
    }
  })
  await test('finding 2: row-less exit 1 stays unknown and can recover', async () => {
    const c = await make('rowless-unknown'); c.setup()
    const first = c.step(1).text
    assert.equal((first.match(/^OUTCOME_UNKNOWN name=/gm) || []).length, 3)
    assert.doesNotMatch(first, /MANIFEST_REFUSED|^REFUSED /m)
    assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
  })
  await test('finding 3: digest conflict settles the earlier unknown job, including nameless waits', async () => {
    for (const scenario of ['unknown-c,digest-conflict-c,no-name', 'rowless-unknown,digest-conflict-c,no-name']) {
      const c = await make(scenario); c.setup(); c.step(1)
      const second = c.step(1).text
      assert.match(second, /SUBMITTED name=c job_id=job-c created=False/)
      assert.doesNotMatch(second, /REFUSED name=c/)
      assert.match(second, /JOBS=3\nUNSETTLED=0/)
      assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
    }
    const fresh = await make('digest-conflict-c'); fresh.setup()
    assert.match(fresh.step(1).text, /REFUSED name=c/)
  })
  await test('finding 4: failed setup writes nothing in the caller directory and the prompt stops', async () => {
    const c = await make('', 'rq-blocked', 'blocked/child')
    writeFileSync(join(c.root, 'blocked'), 'not a directory')
    const r = c.step(0)
    assert.notEqual(r.status, 0)
    assert.doesNotMatch(r.text, /SETUP_OK/)
    assert.deepEqual(readdirSync(c.root).filter(name => name.startsWith('rq-blocked.')), [])
    assert.match(c.prompt, /If step 0 does not print SETUP_OK, stop/)
    assert.equal(c.count('run'), 0)
    assert.notEqual(c.step(1).status, 0)
    assert.equal(c.count('run'), 0)
  })
  await test('finding 5: submit-down without request id has a safe RESET for a space and quote', async () => {
    const c = await make('submit-down', null, "my dir/st'x")
    writeFileSync(join(c.root, 'my'), 'keep')
    c.setup()
    writeFileSync(join(c.args.stateDir, 'keep.txt'), 'keep')
    assert.doesNotMatch(c.step(1).text, /RETRY_STEP1/)
    const second = c.step(1).text
    assert.equal(c.count('run'), 1)
    const command = second.match(/^RESET (.*)$/m)[1]
    const reset = c.shell(command)
    assert.equal(reset.status, 0, reset.stderr)
    assert.equal(readFileSync(join(c.root, 'my'), 'utf8'), 'keep')
    assert.deepEqual(readdirSync(c.args.stateDir), ['keep.txt'])
  })
  await test('finding 6: refused entries do not trigger resubmission', async () => {
    const c = await make('refuse-b'); c.setup()
    assert.match(c.step(1).text, /REFUSED name=b/)
    const second = c.step(1).text
    assert.doesNotMatch(second, /RESUBMIT|RETRY_STEP1|RESET/)
    assert.equal(c.count('run'), 1)
    c.setup(); c.step(1)
    assert.equal(c.count('run'), 1)
    assert.doesNotMatch(c.wait(), /DONE name=b/)
    const mixed = await make('refuse-b,unknown-c'); mixed.setup(); mixed.step(1)
    assert.match(mixed.step(1).text, /RESUBMIT names=c\n/)
  })
  await test('finding 7: citations match the base and cache-write totals match the CSV', () => {
    const report = readFileSync(join(harness, '../../2026-09-22-fanout-workflow.md'), 'utf8')
    const workflow = readFileSync(script, 'utf8')
    assert.doesNotMatch(report + workflow, /2441fbea/)
    assert.match(workflow, /81d58fe9/)
    for (const line of report.split('\n').filter(line => /daemon\.py:(1087|1419|1440|1867)/.test(line))) {
      assert.match(line, /81d58fe9/)
    }
    assert.match(report, /policy\.json/)
    const records = readFileSync(join(harness, '../dispatcher-usage.csv'), 'utf8').trim().split('\n').slice(1).map(row => row.split(','))
    const total = records.reduce((sum, row) => sum + Number(row[4]), 0)
    const slices = records.slice(4).reduce((sum, row) => sum + Number(row[4]), 0)
    assert.equal(total, 1320152); assert.equal(slices, 1236718)
    assert.match(report, /1,320,152[^.]*30 turns/)
    assert.match(report, /1,236,718[^.]*13 slices/)
  })
  await test('failed-a: exit 1 with rows is a failed job, with no daemon retry', async () => {
    const c = await make('failed-a'); c.setup(); c.step(1)
    const output = c.wait()
    assert.match(output, /WAIT_RC=1[\s\S]*DONE name=a job_id=job-a state=failed rc=1/)
    assert.doesNotMatch(output, /DAEMON_UNREACHABLE|RETRY_SLEPT/)
  })
  await test('wait retry sleep respects the time remaining in the slice', async () => {
    const c = await make('wait-down-first'); c.setup(); c.step(1); c.step(2)
    const helper = `${c.args.requestId}.fanout.py`
    const r = c.shell(`cd '${c.args.stateDir}' && { T0=$(date +%s); SUBFLEET_FANOUT_RETRY_S=60 python3 -u ${helper} post-wait 69 $((T0 - 588)); }`)
    assert.equal(r.status, 0, r.stderr)
    const slept = Number(r.stdout.match(/RETRY_SLEPT=(\d+)/)[1])
    assert.ok(slept >= 0 && slept <= 2, r.stdout)
    assert.match(r.stdout, /PENDING_COUNT=3/)
  })
  for (const scenario of ['wait-down-first', 'lost-c', 'unknown-id', 'no-name']) {
    await test(`retained scenario: ${scenario}`, async () => {
      const c = await make(scenario); c.setup(); c.step(1)
      const output = c.wait()
      if (scenario === 'wait-down-first') assert.match(output, /DAEMON_UNREACHABLE rc=69[\s\S]*RETRY_SLEPT=0/)
      if (scenario === 'lost-c') assert.match(output, /DONE name=c job_id=job-c state=lost rc=125/)
      if (scenario === 'unknown-id') assert.equal((output.match(/^UNKNOWN name=/gm) || []).length, 3)
      if (scenario === 'no-name') assert.match(output, /DONE name=c job_id=job-c state=succeeded/)
    })
  }
  await test('space and quote state directory: complete submit/wait cycle', async () => {
    const c = await make('', 'rq-quoted', "my dir/st'x"); c.setup(); c.step(1)
    assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
  })
  await test('helper and manifest checksum tampering stops setup', async () => {
    for (const body of ['KEY =', '{"label": "t"']) {
      const c = await make()
      const r = c.step(0, c.commands[0].replace(body, body + ' '))
      assert.notEqual(r.status, 0)
      assert.match(r.text, /CHECKSUM_MISMATCH/)
      assert.doesNotMatch(r.text, /SETUP_OK/)
    }
  })
  await test('changed manifest under one request id stops with STATE_MISMATCH', async () => {
    const c = await make(); c.setup()
    const changed = await renderWorkflow(script, { ...c.args, items: c.args.items.map(j => ({ ...j, tier: 'hard' })) })
    const r = c.step(0, changed.commands[0])
    assert.notEqual(r.status, 0)
    assert.match(r.text, /STATE_MISMATCH/)
  })
  await test('separate batches in one output directory use separate state keys', async () => {
    const c = await make('', null)
    const a = await renderWorkflow(script, [c.args.items[0]])
    const b = await renderWorkflow(script, [c.args.items[1]])
    for (const r of [a, b]) { assert.equal(c.step(0, r.commands[0]).status, 0); assert.equal(c.step(1, r.commands[1]).status, 0) }
    assert.notEqual(a.out.state_key, b.out.state_key)
    assert.equal(c.count('run'), 2)
  })
  await test('relaunch without request id keeps SKIP and never resubmits', async () => {
    const c = await make('', null)
    writeFileSync(c.args.items[0].outPath, 'already delivered')
    assert.match(c.setup().text, /SKIP name=a/)
    c.step(1); c.setup()
    assert.match(c.step(1).text, /SKIP name=a out_bytes=17/)
    assert.equal(c.count('run'), 1)
  })
} finally {
  execFileSync('python3', ['-c', 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)', temp])
}
console.log(`${passed} checks passed; ${failures} failed${lockedPeers ? `; ${lockedPeers} overlapping peer(s) reported LOCKED under load and were checked by relaunch` : ''}`)
if (failures) process.exitCode = 1
