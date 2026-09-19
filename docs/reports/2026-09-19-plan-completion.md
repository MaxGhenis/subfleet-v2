# Plan completion audit, 2026-09-19

This change builds on `b4b23fb`, the existing v2 implementation, against
`docs/plan.md` and its binding acceptance contract. It closes implementation
and verification gaps found by three parallel reviews. It does not certify
the operational canary, shadow period, or production cutover.

| Contract | Gap closed | Regression coverage |
|---|---|---|
| C-6.2 | Concurrent submissions sharing a request ID cannot overwrite each other's staged prompt. Invalid IDs are rejected rather than truncated. | `tests/unit/test_cli.py` |
| C-6.5, C-13 | Writable jobs recheck their branch at reservation and launch; salvage compares against the actual pre-attempt working tree. | `tests/fake/test_workspace_contract.py`, `tests/unit/test_salvage.py` |
| C-4.4 | An exit receipt without a return code becomes a recorded loss. | `tests/fake/test_workspace_contract.py` |
| C-12.3, C-12.4, C-23.32 | Native resume uses the source session, model, lane, exclusions, and workspace; imported session IDs are recovered without rewriting history. | `tests/fake/test_resume_contract.py`, `tests/fake/test_resume_cli.py` |
| C-12.3, C-12.4, C-23.55 | Resume and revive take a durable lease on their lane and native session, including for read-only work. Quarantine retains it until resolution. | `tests/fake/test_resume_contract.py` |
| C-13.4 | Queued and running continuations protect their source worktree from retention. | `tests/fake/test_resume_contract.py` |
| C-15.3 | Reading a job no longer acknowledges another session's notices. | `tests/fake/test_state_contract.py` |
| C-11.2, C-23.37 | The compatibility picker uses the daemon evaluator; stranded Claude capacity has the planned preference, with model strength defined in policy. | `tests/unit/test_scheduler.py`, `tests/unit/test_policy_support.py` |
| C-9.9, C-11.7 | Reserved headroom requires a complete, recognizable usage snapshot. Unknown scoped evidence stays unknown; a later complete snapshot may remove a scoped bucket. | `tests/unit/test_claude_usage.py`, `tests/unit/test_scheduler.py`, `tests/e2e/test_reserve.py` |
| Plan amendment 1, C-23.35 | `enroll` remains a permanent spelling, and retired sessions stay out of every listing. | `tests/unit/test_compat.py`, `tests/unit/test_sessions_cli.py` |
| C-23.14 | Handoff omits results of additional keychain credential-reading spellings. | `tests/unit/test_sessions_handoff.py` |
| C-20.4 | Release checks verify actual execution against the canary clone and reject incomplete evidence. Canary retries reuse frozen prompts; numeric gates enforce their thresholds. | `tests/unit/test_soak_tools.py` |

The fake usage endpoint now computes future reset times when called. Its old
September 2026 timestamps caused two reserve end-to-end tests to expire while
the production freshness checks correctly rejected their readings.

The new GitHub Actions workflow builds distributions and runs the default suite
on macOS with Python 3.12 and 3.14. It explicitly disables live tests. Actions
are pinned to verified commit IDs, and uv 0.12.17 was verified against the
official `astral-sh/uv` release API on this date.

The [numeric measurements](2026-09-19-release-measurements.md) passed with a
300-job store and 200 calls: cached status p95 83 ms, submit p95 119 ms, and
daemon SIGKILL recovery 3.4 seconds with one succeeded attempt.

The source distribution and wheel built successfully. A fresh environment
installed the wheel and ran the dispatcher, daemon, gate, and sessions entry
points with their version/help arguments.

Current operational evidence and outstanding gates are recorded in
[`docs/release-gates.md`](../release-gates.md). The original checkout's
uncommitted coordination notes and the installed v1 command were preserved.
