import { readFileSync } from 'node:fs'
const src = readFileSync(process.argv[2], 'utf8').replace('export const meta', 'const meta')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const run = async (args, agentImpl) => {
  const logs = []
  const fn = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', 'budget', 'workflow', src)
  const out = await fn(args, agentImpl, async t => Promise.all(t.map(x => x().catch(() => null))), null, () => {}, m => logs.push(m), { total: null }, null)
  return { out, logs }
}
const ok = { task: 'review', tier: 'standard', dir: '/Users/x', promptPath: '/p.md', outPath: '/o/a.md', name: 'a' }
const cases = [
  [[], 'empty'], ['x', 'string'],
  [[{ ...ok, task: 'nope' }], 'bad task'], [[{ ...ok, dir: 'rel' }], 'relative dir'], [[{ ...ok, dir: '/tmp/x' }], 'tmp dir'],
  [[{ ...ok, name: 'bad name' }], 'bad name'], [[ok, ok], 'dup'], [{ items: [ok], label: 'bad label' }, 'bad label'],
  [{ items: [ok], requestId: 'has space' }, 'bad requestId'], [{ items: [{ ...ok, sandbox: 'rw' }] }, 'bad sandbox'],
  [{ items: [{ ...ok, model: 'gpt 6' }] }, 'bad model'],
]
for (const [bad, why] of cases) { try { await run(bad, async () => null); console.log('NO THROW:', why) } catch (e) { console.log('throws (' + why + '):', e.message.slice(0, 110)) } }
const r1 = await run({ items: [{ ...ok, dir: '/tmp/x' }], allowTmp: true, model: 'opus' }, async (prompt) => ({ setup_ok: true, already_submitted: false, submit_calls: 1, submit_rc: 0, request_id: '', wait_loops: 0, last_wait_rc: 0, notes: '', jobs: [] }))
console.log('allowTmp+model ok ->', JSON.stringify(r1.out.jobs[0]).slice(0, 160))
const dead = await run({ items: [ok] }, async () => null); console.log('dead agent ->', JSON.stringify(dead.out), dead.logs)
const full = await run({ items: [ok, { ...ok, name: 'b', outPath: '/o/b.md' }, { ...ok, name: 'c', outPath: '/o/c.md' }], requestId: 'rq-1' }, async () => ({ setup_ok: true, already_submitted: true, submit_calls: 1, submit_rc: 0, request_id: 'rq-1', wait_loops: 3, last_wait_rc: 125, notes: '',
  jobs: [{ name: 'a', job_id: 'ja', state: 'succeeded', rc: 0, out_bytes: 9, notes: '' }, { name: 'c', job_id: 'jc', state: 'lost', rc: 125, out_bytes: 0, notes: '' }] }))
console.log('shaping ->', JSON.stringify(full.out.summary), '| b:', JSON.stringify(full.out.jobs[1]), '| logs:', JSON.stringify(full.logs))
