import { readFileSync, writeFileSync } from 'node:fs'
const [,, script, outDir, argsJson] = process.argv
const src = readFileSync(script, 'utf8').replace('export const meta', 'const meta')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const run = async (args, agentImpl) => {
  const logs = []
  const fn = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', 'budget', 'workflow', src)
  const out = await fn(args, agentImpl, async t => Promise.all(t.map(x => x().catch(() => null))), null, () => {}, m => logs.push(m), { total: null }, null)
  return { out, logs }
}
let captured = null
const stub = { setup_ok: true, already_submitted: false, submit_calls: 1, submit_rc: 0, request_id: 'r', wait_loops: 1, last_wait_rc: 0, notes: '', jobs: [] }
const { out, logs } = await run(JSON.parse(argsJson), async (prompt, o) => { captured = { prompt, o }; return stub })
const p = captured.prompt
const cut = (a, b) => p.split(a)[1].split(b)[0]
writeFileSync(`${outDir}/step0.sh`, cut('prints CHECKSUM_OK then SETUP_OK:\n', '\n\nIf it prints CHECKSUM_MISMATCH'))
writeFileSync(`${outDir}/step1.sh`, cut('JOBS and UNSETTLED:\n', '\n\nRun step 1 again'))
writeFileSync(`${outDir}/step2.sh`, cut('and PENDING_COUNT:\n', '\n\nRepeat step 2'))
console.log('rendered; agent model', captured.o.model, '| logs:', JSON.stringify(logs), '| out summary:', JSON.stringify(out.summary))
