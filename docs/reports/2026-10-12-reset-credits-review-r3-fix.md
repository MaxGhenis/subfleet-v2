# PR #179 fix round 3

Base: `0efe6254f58c16b4c87e08446259397eb0eb189a`.
Implementation: `ba7d8633e3103da7b2a8350a56d2e20d0fd91da5` on
`fix/reset-credit-demand-only-217` in workspace-local `.git-local` metadata.
The shared Git index is outside the writable sandbox. No remote was contacted.

## Change

The default consume transport builds an opener with an HTTPRedirectHandler
whose `redirect_request` returns None. urllib now exposes the original 3xx as
HTTPError; the existing classifier publishes `unknown`. A later GET can no
longer turn a possibly spent POST into `failed` and permit another credit.

The adapter's other HTTP requests only read usage or list gifted entitlements;
consume is its only mutating HTTP endpoint. The consume guard therefore covers
all HTTP requests this adapter owns that spend or claim a credit. C-18.1 now
states that the original response controls the verdict, and C-23.16(d)'s
consume table explicitly says redirects are never followed.

Ported the three reviewer files from `review-r3-probes/` into:

- `tests/unit/test_reset_credit_review_r3.py`: the 14 answer edges, socket-boundary
  failures, six real-urllib redirect cases (302/303/307 × 409/429), and the
  provider-observed interval oracle control. Redirect cases also require the
  original HTTP status, no redirected GET, one POST body, retained closures,
  and `unsettled-action` at +60 seconds.
- `tests/unit/test_reset_credit_review_r3_mutants.py`: the original eight
  mutation cases now require the original regressions to raise AssertionError,
  plus four cases that restore urllib redirect following and require the new
  transport regressions to reject it. monkeypatch restores each mutation.
- `tests/unit/reset_credit_review_r3_budget.py`: the review's pytest hold-budget
  plugin, loaded in every run; this helper contains no collected tests.

## Validation

All twelve lock attempts timed out with exit 75 before pytest started: the
first transport and round-two attempts waited 120 seconds each; the remaining
ten attempts waited 30 seconds each. No lock hold was acquired, so no test or
Hypothesis example executed. Every runner and its cleanup trap has exited.
The brief's lock-queue exception applies; the host must complete these runs.
No prior review's test results are credited to this fix job.

| File / selection | Passed | Failed | Execution |
|---|---:|---:|---|
| `tests/unit/test_reset_credit_review_r2.py` | 0 | 0 | Unrun: lock timeout |
| `tests/unit/test_reset_credit_claim_timing.py` | 0 | 0 | Unrun: lock timeout |
| `tests/unit/test_reset_credit_review_r1.py` | 0 | 0 | Unrun: lock timeout |
| `tests/unit/test_timers_reset_credits.py` | 0 | 0 | Unrun: lock timeout |
| `tests/unit/test_reset_credit_demand.py` | 0 | 0 | Unrun: lock timeout |
| `tests/fake/test_reset_credit_incident.py` | 0 | 0 | Unrun: lock timeout |
| `tests/unit/test_reset_credit_review_r3.py` | 0 | 0 | Unrun: both attempts timed out |
| `tests/unit/test_reset_credit_review_r3_mutants.py` | 0 | 0 | Unrun: lock timeout |
| `tests/unit/test_reset_credit_properties.py` | 0 | 0 | Unrun: lock timeout |
| `tests/unit/test_test_module_names.py` | 0 | 0 | Unrun: lock timeout; equivalent static check passed |
| Raw redirect-following mutant: four 302/303 × 409/429 regressions | 0 | 0 | Unrun: lock timeout; mutation proof unverified |
| `tests/unit/reset_credit_review_r3_budget.py` | 0 | 0 | Helper: no collected tests |

The generated properties retain their default example counts (100 each);
completed examples: **0**. The mutant searches retain their default 300-example
budgets; completed searches: **0**. Before/after transport assertions and all
mutation-kill assertions remain unverified by this job.

Host replay: run each table file separately under the prescribed lock command,
loading `-p hypothesis.extra.pytestplugin -p tests.unit.reset_credit_review_r3_budget`.
The run-only helper `.uv-cache/r3-runs/run-suite.zsh` also configures offline
execution, the entrypoint alarm, defaults, and cleanup. Raw mutation replay uses
`PYTHONPATH="$PWD/.uv-cache/r3-runs" /bin/zsh .uv-cache/r3-runs/run-suite.zsh tests/unit/test_reset_credit_review_r3.py -k 'default_transport and not 307' -p follow_redirects`;
all four selected tests must fail on the original observation assertion.

Static checks: all four changed Python files parse, test-module basenames are
unique, and `git diff --check` passes. The implementation commit has the
required co-author trailer and stages only the five named code/contract/test
files.

Each attempt uses its own TMPDIR, a foreground
`/usr/bin/lockf -k -t <queue-seconds> /private/tmp/claude-501/subfleet-suites.lock uv run pytest -q <file>`,
then the prescribed python3/shutil cleanup. Queue waits are bounded separately
from each hold. The copied local pytest entrypoint arms an 18-minute alarm
before importing pytest; the ported plugin retains that deadline. No SQLite
synchronization setting is changed (the store defaults to FULL). Hypothesis's
example overrides are unset: generated properties request their default 100
examples and mutant searches keep their default 300-example budgets.
Cached dependencies were copied into the workspace, and uv uses offline,
no-sync/no-cache mode. All transports and stores in the probes are task-local
fakes. A separate process-local plugin restores redirect following for a raw
run of the four new 302/303 × 409/429 regressions; that run is prepared but unrun, so the requested mutation evidence
is outstanding.

The live reset switch remains off. The shipped policy, live marker, live state,
caller checkout, and live processes are untouched. No background runner or
build is created. Logs, local Git metadata, dependency copies, and run-only
helpers are never staged.

DONE
