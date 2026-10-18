import { readFileSync, writeFileSync } from 'node:fs'
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
export const runWorkflow = async (script, args, agentImpl) => {
  const src = readFileSync(script, 'utf8').replace('export const meta', 'const meta')
  const logs = []
  const fn = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', 'budget', 'workflow', src)
  const out = await fn(args, agentImpl, async t => Promise.all(t.map(x => x().catch(() => null))), null, () => {}, m => logs.push(m), { total: null }, null)
  return { out, logs }
}
export const renderWorkflow = async (script, args) => {
  let captured = null
  const stub = { setup_ok: true, already_submitted: false, submit_calls: 1, submit_rc: 0, request_id: 'r', wait_loops: 1, last_wait_rc: 0, notes: '', jobs: [] }
  const result = await runWorkflow(script, args, async (prompt, o) => { captured = { prompt, o }; return stub })
  // The command is between the step's introduction and its next paragraph.
  // Step 0 contains heredoc bodies, so only the following instruction ends it.
  const commands = [
    captured.prompt.match(/STEP 0\.[^\n]*\n([\s\S]*?)\n\nIf /)[1],
    captured.prompt.match(/STEP 1\.[^\n]*\n([\s\S]*?)\n\nRun step 1 again/)[1],
    captured.prompt.match(/STEP 2\.[^\n]*\n([\s\S]*?)\n\nRepeat step 2/)[1],
  ]
  return { ...result, ...captured, commands }
}

if (import.meta.main) {
  const [,, script, outDir, argsJson] = process.argv
  const { commands, prompt, o, out, logs } = await renderWorkflow(script, JSON.parse(argsJson))
  commands.forEach((command, i) => writeFileSync(`${outDir}/step${i}.sh`, command))
  writeFileSync(`${outDir}/dispatcher-prompt.txt`, prompt)
  console.log('rendered; agent model', o.model, '| logs:', JSON.stringify(logs), '| out summary:', JSON.stringify(out.summary))
}
