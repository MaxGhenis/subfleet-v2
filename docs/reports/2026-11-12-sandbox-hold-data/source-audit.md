# Sandbox hold source audit

This audit covers the working implementation of D-RO-ADMIT1 against starting revision `b51f08f961e1b1dcfb93347eb080f9ddd9a9260d`. All searches stayed inside the assigned worktree. No live state, credentials, provider process or caller checkout was inspected or changed.

## Sandbox submitted, stored and launched

- `subfleet/cli.py:932` sends the explicit caller `-s`, or the `policy` sentinel when omitted. `subfleet/protocol.py:147` documents this sentinel.
- `subfleet/default_policy.json:64` declares `build=workspace-write`, `*=read-only`, and **`review=worktree-write`**. The brief's statement that every other task defaults to read-only omits this existing review exception; the implementation preserves it.
- `subfleet/policy.py:40` keeps explicit choices, resolves policy task/wildcard entries, and maps conditional `worktree-write` authority to workspace-write only for a committed repository outside in-place use. Isolation and gate reviews resolve read-only. `subfleet/daemon.py:3513` supplies the repository and isolation facts.
- `subfleet/daemon.py:3076` resolves policy before validating the sandbox enum. `subfleet/daemon.py:3161` can fall back to read-only when a policy review's uncommitted-copy plan is refused; this happens before the job exists. Explicit caller choices are preserved.
- `subfleet/daemon.py:3305` creates persisted job values from those resolved arguments. `jobs.sandbox` is `TEXT NOT NULL CHECK (sandbox IN ('read-only','workspace-write'))` at `subfleet/store_schema.sql:98`. Current valid state cannot contain an empty sandbox; admission nevertheless treats empty or null legacy/test values conservatively as workspace-write while that hold is active.
- `subfleet/task_holds.py:242` reads `kind, task, sandbox` directly from `jobs` inside the caller's transaction and reads the two possible hold rows. `subfleet/task_holds.py:213` exempts turns and jobs without tasks, uses the stored sandbox, and chooses the later active end. It preserves the earliest start of simultaneously active covering holds for paused clocks.
- `subfleet/daemon.py:2195` obtains the attempt sandbox from `job["sandbox"]`. `subfleet/daemon.py:9797` builds the attempt spec, passed unchanged to the adapter at `subfleet/daemon.py:9837`. Codex emits `--sandbox` with this value at `subfleet/adapters/codex.py:582`; Claude derives permission arguments from it at `subfleet/adapters/claude.py:1416` (resume at `1472`).

## Changes and final source anchors

| Changed path | Current line anchors | Change |
|---|---|---|
| `subfleet/task_holds.py` | `41`, `126`, `175`, `188`, `206`, `213`, `242`, `254` | Separate sandbox lease range; row discriminator; list/count both targets; sandbox release command; composed hold with continuous start; stored sandbox read; reused wait payload. |
| `subfleet/daemon.py` | `284`, `1577`, `1656`, `1728`, `1812`, `1855`, `1862`, `1980`, `1990`, `2047`, `7681` | Read/exclude sandbox range; reuse takes and write-time checks; preserve stale pin pause; target-specific queued-job selection, restore, hold and release; exclude hold leases from capacity census. |
| `subfleet/protocol.py` | `358`, `366` | Optional sandbox target on `TasksArgs`. |
| `subfleet/cli.py` | `569`, `932`, `2426`, `3798`, `3818`, `3833` | Status target rendering; preserve existing submit sandbox source; sandbox tasks payload and output; mutually exclusive task/sandbox CLI and exemption help. |
| `subfleet/offline.py` | `386`, `388` | Include the disjoint sandbox range in offline status. |
| `subfleet/render.py` | `360`, `602` | Render sandbox holds and their release hints through existing status/why machinery. |
| `subfleet/refusals.py` | `317`, `509` | Closed invalid-target refusal; checked sandbox discriminator in conditional ownership refusals. |
| `subfleet/actions.py` | `746`, `749` | Shared write-time held-job check excludes sandbox-held demand and returns `sandbox_held`. |
| `docs/acceptance-contract.md` | `564` | Handwritten D-RO-ADMIT1 extension within C-11.10; existing clauses retain their numbers. |

## Lease reapers and releases, by name

The existing storage analysis in `docs/reports/2026-10-24-task-hold.md:95` was rechecked against all `DELETE FROM leases` statements and `release_leases` calls in `subfleet/`, including an AST function-name inventory. The table distinguishes the intentional operator release from ordinary cleanup. The operator holder is `operator: <note>`; normal attempt, job, probe, enrollment, retention, resume and cwd identities never equal it.

| Function | Final source line(s) | Selector and sandbox effect |
|---|---|---|
| `Store.release_leases` | `subfleet/store.py:933, 934` | Exact supplied holder, optionally an exact key prefix; ordinary holder releases leave the operator row. |
| `Retirement.commit` | `subfleet/retention_archive.py:974` | Exact `retention:<job>` holder. |
| `Retirement._reclaim` | `subfleet/retention_archive.py:1131` | Delegates to exact `retention:<job>` holder release. |
| `Retirement.rollback` | `subfleet/retention_archive.py:1264` | Exact `retention:<job>` holder. |
| `_Pass._recover` | `subfleet/retention.py:703, 706` | Census restricted to `holder LIKE retention:%`; exact selected key+holder deletion. Separately stale `retire:%`/`resume:%` fences. |
| `_Pass._rollback` | `subfleet/retention.py:1075` | Exact `retention:<job>` holder. |
| `Timers._release` | `subfleet/timers.py:611` | Delegates to `Store.release_leases` for a timer/probe holder. |
| `Daemon._enroll_lane_locked` | `subfleet/daemon.py:1412` | Enrollment holder only. |
| `Daemon._release_task` | `subfleet/daemon.py:2084` | **Intentional operator release:** exact selected task or sandbox key; releasing one target does not delete the other. |
| `Daemon.submit` | `subfleet/daemon.py:3468` | Exact `retire:<source>` fence key and `resume:<request>` holder. |
| `Daemon._fence_resume` | `subfleet/daemon.py:3600` | `lease_key LIKE retire:% AND holder LIKE resume:%`. |
| `Daemon._release_worktree_hold` | `subfleet/daemon.py:5581` | Exact `cwd:<job>` holder. |
| `Daemon._keep_for_cwd` | `subfleet/daemon.py:5609` | `worktree:%` keys held by the ending job or attempt. |
| `Daemon._release_ending` | `subfleet/daemon.py:5643` | Ending job/attempt holders; optionally retains worktree keys. |
| `Daemon._recover_then_start_timers` | `subfleet/daemon.py:6004, 6008` | `retire:%`/`resume:%` fence cleanup, then `probe:timer:%` holder census and release. |
| `Daemon._finish_probe` | `subfleet/daemon.py:7061, 7084` | Exact durable probe holder. |
| `Daemon._skip_revive` | `subfleet/daemon.py:9613` | Exact revive job holder. |
| `Daemon._quarantine` | `subfleet/daemon.py:11305` | `lane:%` keys held by the quarantined attempt. |
| `Daemon._resolve_quarantine` | `subfleet/daemon.py:11336` | Quarantined job/attempt holders; optionally retains worktree keys. |
| `Daemon._finalize` | `subfleet/daemon.py:12029, 12045` | Attempt holder restricted to `lane:%`, then job/attempt holders with optional worktree retention. |
| `Daemon._export_locked` | `subfleet/daemon.py:12224` | Exporting job/attempt holders with optional worktree retention. |
| `GateService._consume` | `subfleet/gate/service.py:417` | Exact gate round lease key and holder. |

`subfleet/daemon.py:7092` begins from `holder LIKE probe:%` and therefore never treats an operator row as a probe. No general lease reaper based on `expires_at` exists. Expired operator rows remain stored and hold nothing because `task_holds.in_force_now` checks the clock.

`sandbox-hold:workspace-write` is lexically less than `task-hold:`. It cannot fall within the existing task range `[task-hold:, task-hold;)`. Task and sandbox range reads remain separate. Admission's lease census explicitly excludes both ranges, so releasing a sandbox hold does not look like freed capacity for unrelated waits.

Other lease readers named in the original report were checked: `folders.py`, `resource_leases.py`, `waits.py`, `lanes_transfer.py`, `operations.py`, `capacity.PROBE_LEASES` and `conversations/*` use component key ranges/prefixes or matching job/attempt holders. None grants ordinary cleanup authority over a sandbox hold.

`tests/unit/test_sandbox_hold_reapers.py` adds 26 parameter cases covering future and expired rows through actual Store release, retention orphan/stale-fence recovery, daemon startup/probe recovery, timer release and ending-job release/retention paths, plus disjoint task-range reads. This audit agent ran no pytest commands; root records test results.

## Wait reason, events, CLI, MCP and desktop

The implementation reuses `task-held`, `task.held`, `task.released`, `job.task_held`, `job.task_hold_moved` and `job.task_hold_ended`. Sandbox payloads carry `sandbox` instead of `task`; task payloads retain their existing shape. `task_holds` remains the list/status field and includes both target kinds. No new wait reason requires an additional downstream vocabulary.

`rg -n 'task-held' subfleet app --glob '*.py' --glob '*.swift'` finds production occurrences only in `subfleet/daemon.py`, `subfleet/cli.py`, `subfleet/render.py` and `subfleet/task_holds.py`. There are no literal matches in desktop Swift or MCP-specific source. Desktop decoders retain `wait_reason` as `String?` at `app/Sources/StatusModel.swift:152` and `app/Sources/Protocol.swift:757`; `app/Sources/UIWindow.swift:1213` renders it generically. Repository MCP source is `subfleet/adapters/claude_mcp.py`; it has no wait-reason-specific decoder or literal requiring a new case.

Admission door, reservation and intervening wait/episode writers share the same task-hold machinery. The held state is excluded from idle-admission records and reset-credit demand. Reset action refusal now carries `sandbox_held` for a sandbox target. CLI status rendering, capacity/hold rendering, release hints and structured conditional refusals accept the sandbox discriminator. CLI help explicitly exempts conversation turns and jobs with no task.

## Independent review findings

1. An internal string index originally collided with a valid task literally named `sandbox-hold:workspace-write`. The implementation now uses a typed tuple sandbox index and retains ordinary task string keys.
2. Selecting the later-ending row alone could charge pin grace while an earlier overlapping task hold covered the job. Active composition now preserves the earliest covering start; an already-taken stale episode also retains its first take's earlier start after that earlier row expires. `tests/fake/test_sandbox_hold_continuity.py::test_stale_pin_opening_preserves_the_pause_across_overlapping_holds` has two cases: stale writer at 21 while both holds are active, and at 110 after the task hold expires. The episode starts at 10 due 70; task coverage is 0–100, sandbox coverage 20–200. All 60 seconds remain paused; sandbox release at 120 restores failure at 180. Operator row acquired times remain 0 and 20.
3. A proposed production `_spec` fallback for empty legacy sandbox values was reviewed and rejected: workspace preparation still compares raw job sandbox values, so implicitly launching workspace-write for an invalid empty row could bypass normal isolation preparation. The requested conservative behavior belongs to admission; current schema already rejects empty rows.

## Audit command inventory

All commands were repository-local searches/reads, Git reads, AST/source inspection, documentation/test edits, or compilation. Representative exact commands: `pwd && rg --files -g 'AGENTS.md' -g 'task_holds.py' -g '*2026-10-24-task-hold*' -g 'acceptance-contract.md' -g 'SKILL.md'`; `cat docs/reports/2026-10-24-task-hold.md`; `sed -n '100,143p' docs/reports/2026-10-24-task-hold.md`; `rg -n 'DELETE FROM leases|delete from leases|reap.*lease|lease.*reap' subfleet`; `rg -n 'release_leases\(' subfleet`; `rg -n 'task-held' subfleet app --glob '*.py' --glob '*.swift'`; `rg -n -A 5 '^def _name' subfleet/policy.py`; `git -c core.fsmonitor=false diff -- subfleet/task_holds.py subfleet/daemon.py subfleet/actions.py subfleet/offline.py subfleet/protocol.py`; `python3 -m py_compile tests/unit/test_sandbox_hold_reapers.py`; `python3 -m py_compile tests/fake/test_sandbox_hold_continuity.py`; and `git -c core.fsmonitor=false diff --check -- docs/acceptance-contract.md tests/unit/test_sandbox_hold_reapers.py tests/fake/test_sandbox_hold_continuity.py`. Successful commands returned 0; no pytest was run by this agent. Additional `rg`, `sed` and Python here-document reads inspected the exact functions and SQL listed above. Several early exploratory searches named nonexistent `desktop`, `macos`, `scripts`, `subfleet/schema*`, `subfleet/models.py`, `subfleet/tool*`, `tests/unit/test_store.py` or `tests/fake/test_sandbox_hold_admission.py`, returning 1 or 2 before valid-path retries. Ordinary `git diff --stat ...` and `git log -4 --oneline && git status --short ...` stalled; both tool sessions were interrupted with Ctrl-C (exit 130), and the Git reads succeeded with `-c core.fsmonitor=false`. No process was left running. This audit created no folders outside the worktree.
