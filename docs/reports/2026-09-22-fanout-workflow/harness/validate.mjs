import assert from 'node:assert/strict'
import { execFileSync, spawnSync } from 'node:child_process'
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, unlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { renderWorkflow, runWorkflow } from './render.mjs'

const script = resolve(process.argv[2])
const harness = dirname(fileURLToPath(import.meta.url))
const run = (args, agentImpl) => runWorkflow(script, args, agentImpl)
let failures = 0, passed = 0
const test = async (name, fn) => {
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
  assert.match(rendered.commands[1], /--allow-tmp/)
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
const temp = mkdtempSync(join(tmpdir(), 'fanout-r4-'))
const make = async (scenario = '', requestId = 'rq-test', stateSuffix = 'state') => {
  const root = mkdtempSync(join(temp, 'case-'))
  mkdirSync(join(root, 'bin'))
  mkdirSync(join(root, 'empty-home'))
  copyFileSync(join(harness, 'fake-subfleet.sh'), join(root, 'bin', 'subfleet'))
  chmodSync(join(root, 'bin', 'subfleet'), 0o755)
  const env = { ...process.env, PATH: `${root}/bin:${process.env.PATH}`, FAKE_STATE: root,
    SCENARIO: scenario, SUBFLEET_HOME: join(root, 'empty-home'), SUBFLEET_FANOUT_RETRY_S: '0', SUBFLEET_RUN_DETACH: '0' }
  delete env.PYTHONPATH
  const args = { label: 't', stateDir: join(root, stateSuffix), items: ['a', 'b', 'c'].map(name => ({ ...ok, name,
    promptPath: join(root, `prompt-${name}.md`), outPath: join(root, `out-${name}.md`) })) }
  for (const item of args.items) writeFileSync(item.promptPath, `Review ${item.name}`)
  if (requestId !== null) args.requestId = requestId
  const rendered = await renderWorkflow(script, args)
  const shell = command => spawnSync('zsh', ['-c', 'eval "$1"', 'fanout-harness', command], { cwd: root, env, encoding: 'utf8', timeout: 60000 })
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
      c.env.SCENARIO = 'check-prompts'
      writeFileSync(c.args.items[1].promptPath, 'Restored prompt')
      c.setup()
      assert.match(c.step(1).text, /JOBS=3\nUNSETTLED=0/)
      assert.equal(c.count('run'), 2)
      assert.equal(c.count('runs'), 6)
      assert.match(c.wait(), /DONE name=c job_id=job-c state=succeeded/)
    }
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
console.log(`${passed} checks passed; ${failures} failed`)
if (failures) process.exitCode = 1
