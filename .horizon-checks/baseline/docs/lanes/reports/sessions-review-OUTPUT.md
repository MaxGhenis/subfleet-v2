## Kept

- Order retirement actions by event ID, including retire/unretire/retire within one second (C-23.35).
- Exclude revive attempts from the daemon-created headless-session census (C-23.31).
- Record accepted revive model substitutions through the daemon; refused submissions create no history (C-23.39).
- Preserve bare tickle as a survey and immediate single-session manual tickle, while retaining hook delays and sweep quiet windows (C-17.1, C-23.34).
- Honor `--max 0` for both cold revives and handoffs (C-17.1).
- Return exit 7 for refused JSON revives, matching text output (C-17.3, C-17.4).
- Enforce handoff character caps, including tiny/zero budgets, separators, and fallback explanations (C-23.36).
- Restore the 64 MB handoff cwd lookup when recent transcript tails contain no cwd (original brief's v1 compatibility).
- Acquire the mirror lock before writing health state, preserving running/stalled status during contention (C-23.28).
- Read v1 mirror configuration without writing it; preserve explicit empty overrides and combine saved/CLI exclusions (C-17.1, original brief).

## Dropped

- JSON `LIKE` prefilter: its compact pattern misses the daemon's spaced JSON, breaking nudge reservations and retirement lookup; parsed matching remains.
- Claude-only revive model rejection: no clause authorizes it. All six affected tests explicitly request `astra`; model resolution was unchanged (C-23.39, C-23.54).
- Same-interruption revive ban and its tests: C-23.33 dedupes nudges; C-23.55 limits concurrent revives through live leases, not later retries.
- Blanket delay/quiet bypass for named sessions: hook wakes, muster, and multiple-session sweeps still require C-23.34 checks.
- Mirror filtering of explicit empty flags and replacement of saved exclusions: both differ from v1's override/combination behavior.

All eight original failing tests were correct. Three now pass; the five that
exercise probe admission reach the existing containment checks and are blocked
by process inspection restrictions, as on the unmodified baseline. No contract
clauses were edited, and no unsupported refusal was added.

## Tests

Commands used the local venv/cache with `UV_OFFLINE=1 UV_NO_SYNC=1` and
`PATH=/usr/sbin:/sbin:$PATH`. Offline `uv sync --group dev` could not resolve the
project's hatchling build requirement; copying the existing main cache and
`uv sync --group dev --no-install-project` installed the test dependencies.

| Command | Result | Time |
| --- | --- | --- |
| Initial `uv run pytest -q tests/unit/test_sessions_*.py tests/fake/test_sessions_end_to_end.py` | 231 passed, 8 failed, 1 error | 34.09s |
| Final `uv run pytest -q tests/unit/test_sessions_*.py tests/fake/test_sessions_end_to_end.py` | 256 passed, 5 failed, 1 error | 35.40s |
| Final `uv run pytest -q` | 3262 passed, 131 failed, 66 skipped, 1 error | 73.66s |
| Baseline `38a7a9a`: lane venv's `python -m pytest -q` in an unchanged `git archive` export | 3232 passed, 131 failed, 66 skipped, 1 error | 75.20s |
| `uv run pytest -q tests/unit/test_sessions_cli.py tests/unit/test_sessions_nudge.py` | 86 passed | 0.54s |
| `uv run pytest -q tests/unit/test_sessions_handoff.py` | 62 passed | 3.85s |
| `uv run pytest -q tests/unit/test_sessions_mirror.py` | 33 passed | 1.70s |

The final and baseline full suites have **identical sets of 132 failed/error test
IDs**, with **30 additional passing tests** on this branch. This is not a fully
green validation. Unix socket binds fail with `PermissionError: Operation not
permitted`; probe containment reports group, descendant, and marker enumeration
unavailable. Existing process-inspection skips also remain. No guard or sandbox
restriction was bypassed, and no tests were weakened to hide these failures.

`git diff --check` passes. Full logs are `/tmp/sessions-review-final-full.log`,
`/tmp/sessions-review-final-targeted.log`, and
`/tmp/sessions-review-baseline-full.log`.

## Open questions for the integrator

- Run both required suites with permitted socket/process inspection to establish the required green result before merging. All work is committed on `lane/sessions-review`; nothing was pushed.
- The salvaged substitution audit uses a separate RPC after submission. If that RPC fails, an accepted job can remain queued without its audit and the client can fail before returning its ID. Atomic submission/audit would require extending the submit/protocol seam beyond this review's touched daemon seams.
