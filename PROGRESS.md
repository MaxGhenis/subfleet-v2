# Lane progress: cutover-compat

Brief: `docs/lanes/cutover-compat.md`. Branch `lane/cutover-compat`.

## State

| Step | State |
|---|---|
| Read the contract, plan, v2 seams, v1 surface | done |
| `docs/reference/claude-hooks.md` (fetched + binary check) | done |
| Compatibility case harvest from the v1 sources | in progress |
| `subfleet/compat.py` + `tests/fixtures/compat/cases.json` | not started |
| `subfleet/hooks.py` + `subfleet hook <event>` | not started |
| `subfleet/notify_push.py` (layer 4) | not started |
| `subfleet/doctor.py` + `doctor` additions | not started |
| `tests/unit/test_compat.py`, `test_hooks.py`, `test_doctor.py` | not started |

## Done

- Read `docs/acceptance-contract.md` §6.7, §15, §16, §17; `docs/plan.md`
  amendment 1; plan B rev 4 "Notices and waiting"; `docs/reports/D-surface.md`
  §3 (env inventory) and §4 (agent contract).
- Read the v2 seams: `subfleet/cli.py` (parser, `rewrite_aliases`,
  `_apply_deprecations`, `doctor_checks`), `subfleet/client.py`,
  `subfleet/protocol.py` (`OPS`, `NoticeArgs`), `subfleet/daemon.py`
  (`notice.pending` / `notice.ack` / `wait` handlers, `_notice` text shape),
  `subfleet/store.py` + `store_schema.sql` (`notices` table), `subfleet/offline.py`.
- Read the v1 surface read-only: `README.md`, `subfleet/cli.py` (verb table and
  the eight hidden verbs), `subfleet/delegate.py` `_parser()` (the real `run`
  flags), `subfleet/hooks.py`, `bin/subfleet-hook`, `subfleet/notify.py`
  (`render_pending`, `push_to_session`, `find_session`, `peer_token`,
  `envelope`), `~/.claude/settings.json` (read-only), `~/.claude/CLAUDE.md`
  "Model routing".
- Wrote `docs/reference/claude-hooks.md` from the fetched hooks page, plus a
  local measurement of the installed 2.1.260 binary.

## Findings that shape the build

1. `asyncRewake` hooks "cannot add context or influence decisions on successful
   completion" — so layer 2 must exit **2** with the notice on **stderr**, and
   exit 0 silently when it has nothing. Confirmed in the fetched page.
2. `SessionStart` / `UserPromptSubmit` exit 2 **blocks the session** / **erases
   the prompt**. Those two events must therefore exit 0 and write to stdout.
3. `session_start_reason` (documented) does not appear in the installed 2.1.260
   binary; v1 reads `source`. Same split for `tool_response` vs `tool_result`
   and `prompt` vs `user_prompt`. Hooks read both spellings.
4. v1's `subfleet run` is a pass-through to `delegate.main()`, so the real v1
   `run` flag set is `delegate.py:_parser()`, not `cli.py`.

## Next

Harvest every v1 invocation into `tests/fixtures/compat/cases.json`, then build
`compat.py` against it.
