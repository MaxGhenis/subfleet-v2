# PR #179 fix round 2

Base: `5c11d18e62fd`. Work is confined to the assigned workspace. The live switch remains off.

Status: both fixes are implemented. HTTP fix commit: `80ebb4e22`; checker and
clock fix commit: `22356d20f`; additional outcome coverage: `5015aa22d`.
Validation is incomplete: all five attempts timed out without acquiring the
shared lock. No pytest process started and no property example executed.

Required work:

- Classify every consume HTTP outcome conservatively; document the status table in C-23.16 and cover each row.
- Exclude intents proved unsubmitted from the spend oracle; reproduce the 61-second durable-claim delay and control fake-provider transport deadlines in generated worlds.
- Show both regressions failing at the base and passing after the fixes.
- Execute the review's property example counts, including at least one FULL seed of each property, under separate shared-lock holds shorter than 20 minutes.

## Consume outcome table

| Answer | Action class | Basis |
|---|---|---|
| 2xx, `code=reset`, positive integer `windows_reset` (excluding booleans) | `confirmed` | Recognized reset confirmation |
| Other 2xx, including missing/malformed JSON or unfamiliar fields | `unknown` | May have spent |
| Any 3xx, including a reset-shaped body | `unknown` | A redirect proves no refusal |
| 409 with `error.code=already_redeemed` or `already_consumed`; 429 with `error.code=rate_limited`; body contains only `error`, with `code` and optional string `message` | `failed` (`refused`) | Explicit refusal of this consume |
| Every other 4xx, including empty/unrecognized bodies, 408, conflicting reset fields, or a refusal code on the wrong status | `unknown` | No proof of an unspent credit |
| Any 5xx, regardless of body | `unknown` | May have spent before answering |
| Any other HTTP status outside 2xx–5xx | `unknown` | Unrecognized response |
| No credential, inhibit marker, or deadline/cancel/occupied slot before the call starts | `failed` (`no-auth`, `inhibited`, `aborted-before-submission`) | Nothing submitted |
| Timeout, connection failure, or unexpected exception after the call starts | `unknown` | Submission may have occurred |

Unknown actions block every later consume until a fresh successful account usage
read reconciles them open. Only 2xx responses can directly confirm a reset.

## Execution

Three base regression commands each waited 300 seconds and exited 75
without executing any tests (about fifteen minutes queued). The last two used a workspace-local
`git archive` of `5c11d18e62fd`, with the same six regression assertions copied
in. The run-only budget plugin checks and prints the imported production and
checker paths, ensuring the base reproduction cannot import the edited head.
Each selected all five redirect cases and the exact delayed-claim world with FULL.
The head regression attempt (FULL, seed 179204, 44 selected cases) also waited
300 seconds and exited 75 without executing a test. The selected switch-off
property attempt (FULL, seed 179002, 100 examples) also waited 300 seconds and
exited 75 before test startup. Total queue time was about twenty-five minutes;
no shared-lock hold was acquired. Smaller FULL seeds
can count toward the requested totals; any remainder would use separately
reported seeds and modes. No prior-round counts are credited to this round.
Each pytest hold has an 18-minute interrupt and 19-minute termination watchdog,
and its own TMPDIR with the prescribed Python cleanup.

The shared Git directory is outside the writable sandbox. Commits use
workspace-local `.git-local` metadata, branch `fix/reset-credit-demand-only-217`,
with the supplied base as its parent and read-only alternate objects. No remote
or caller checkout is changed.

Results, exact counts, FULL/OFF modes, commit heads, and unrun work will be
recorded here as execution completes.

## HTTP fix

Consume responses use a distinct `refused` outcome only for the recognized
409/429 refusals above. Generic read endpoints retain their existing diagnostic
statuses. `http-error` is no longer a no-spend exemption in the action classifier.
The outcome property includes the five redirect statuses, empty 403, 408,
reset-shaped 409, and conflicting 429. The round-2 file checks each table row
through real intent/claim/publication and the next evaluation at +60 seconds;
its adapter checks cover every integer status 100–699 through both returned
responses and raised `HTTPError`, with ten body shapes (12,000 intended fake
requests; none executed here). Refusal expectations are a declarative mapping
of each body to its accepted status, independent of the implementation predicate.
Explicit cases also verify deadline, cancellation and occupied-slot aborts,
missing credentials at the consume boundary, and timeout, dropped-connection
and unexpected exceptions after submission. The existing marker-after-claim
regression is included in the targeted head command.

## Spend checker and timing

The world provider records POSTs independently of action state. `play` now
applies spend eligibility, reset-room and interval assertions, and advances
`model.last_spend`, only for an action whose account had a POST in that
evaluation. Every intent still counts toward the one-action-per-evaluation
assertion and the switch/marker assertions. The generated provider always
spends on its POST; a failed unsubmitted intent cannot spend. This avoids
using the production outcome classifier as the oracle for submission.

`test_full_delayed_claim_has_no_submission_and_no_interval_violation` keeps the
reviewer's exact two-lane world and advances the transport clock by 61 seconds
after each FULL claim commit, without sleeping. It requires two failed intents,
zero POSTs, and zero violations. The base checker instead reports the second
intent as a spend ten minutes after the first (source-traced; execution pending).
The regression now lives in `test_reset_credit_claim_timing.py`; the archived
base assertion is identical and remains in the copied round-2 file.

The generated-world, round-1 and round-2 modules use a fixture that replaces
only `subfleet.actions.time` with a fixed monotonic clock. Fake HTTP callbacks
finish synchronously from the test's point of view, and no elapsed host commit
time can expire their transport deadlines. Dedicated deadline/cancel regressions
explicitly supply their own moving transport clock. A separate FULL regression
adds 61 seconds to the host monotonic clock at each claim and requires steady
and restarted worlds to both decide `confirmed`, then `interval-blocked`.

No other property verdict depends on commit timing after this change:

- Scheduling, freshness, holds, reset-room and minimum intervals use the world's
  explicit UTC step clock; generated publications receive that same clock.
- Switch and marker state, demand routes, submitted accounts, and action counts
  come from world inputs, fake-provider observations, and committed rows.
- Restart comparison has the same inputs and controlled transport deadlines on
  both executions; host fsync duration cannot select different timeout paths.
- Outcome and interval properties also use the controlled transport clock;
  simulated timeouts are explicit fake-provider exceptions, and daemon-clock
  advances are explicit inputs.
- The switch-off differential sends no HTTP. Its histories, overrides, usage
  settlements and row comparisons all use explicit UTC times.
- Hypothesis assertion deadlines are disabled; example and mutant-search counts
  are explicit. Its real clock is not replaced by the property fixture.

FULL versus OFF can still change runtime and crash durability. These tests
restart components and do not simulate process crashes or power loss; no
durability equivalence is claimed. Database failures or nonterminating fake
providers would cause errors or interruptions, not timing-dependent invariant verdicts.

## Regression evidence and outstanding execution

| Regression | Before at `5c11d18e62fd` | After with these fixes |
|---|---|---|
| Redirect 301/302/303/307/308 with reset body, next evaluation +60 seconds | Reviewer's executed observations: `failed`, `failed`, two POSTs, all five statuses. This job's three reproduction attempts were excluded by the lock. | Expected assertion: `unknown`, `unsettled-action`, one POST. Added regression; passing execution **unverified** because the head run never acquired the lock. |
| FULL claim commit adds 61 seconds; two evaluations ten minutes apart | Reviewer's executed observation: two failed unsubmitted intents, zero POSTs, false interval violation; OFF hid it. This job's reproduction attempts were excluded by the lock. | Exact copied assertion requires two failed intents, zero POSTs, zero violations. Its AST matches the archived base case. Passing execution **unverified**. |
| FULL host commit time versus restart equivalence | The review demonstrated that elapsed commit time could select a different deadline path. | New regression requires identical `confirmed`, `interval-blocked` decisions after 61-second host-clock advances; execution **unverified**. |

The before observations come from the supplied `review-r2-full.md` and
`review-r2-probes/pr179_review_r2_probes.py`. They are reviewer evidence, not
fresh executions by this job. No fresh failing or passing regression verdict is
claimed. The before/after execution requirement remains open for the host.

| Property | Requested examples | Completed FULL | Completed OFF |
|---|---:|---:|---:|
| Every rule over generated worlds | 5,000 | 0 | 0 |
| Restart before every step | 2,000 | 0 | 0 |
| Real demand and route room | 2,000 | 0 | 0 |
| Switch-off differential against release/217 | 1,000 | 0 | 0 |

The two round-1 outcome/interval properties (100 examples each), fresh mutant
searches (300-example search budgets), the expanded head regressions (55 selected cases), complete round-1/unit/incident suites,
whole suite, alternate interpreters, frontend/e2e, and process-crash/power-loss
checks are unrun in this job. All 10,000 requested examples remain unrun. No OFF
run was attempted. This is the brief's shared-lock queue exception; the host
must complete the fresh before/after proofs and the requested FULL/OFF counts.

Completed static checks: all seven changed Python files parse; local
`git diff --check` passes; the delayed-claim assertion matches between the
archived base and head; all implementation commits have the required trailer.
Only named source, test, contract, and report files are staged. Logs, evidence
JSON, caches, plugins and archives are never staged.

## Host follow-up

Run the two round-2 files plus the marker-after-claim regression under one
prescribed hold (55 cases at this head). The base archive retained in this
workspace selects the five redirect assertions and exact delayed-claim
assertion against unmodified `5c11d18e62fd` code. Its run-only plugin checks
imported paths before testing, and the delayed assertion's AST matches the
head. No regression verdict should be inferred from queue timeouts.

Complete the property totals in the table, with at least one FULL seed for each
of the four properties and separate documented OFF seeds for any remainder;
use chunks that complete inside the hold budget and report Hypothesis's actual
passing/invalid counts. Complete the round-1 outcome/interval properties and
unit/incident regressions as well. The uncommitted run-only helper files are in
`.uv-cache/r2-runs`; none form part of repository history.

Every attempted run used its own prescribed TMPDIR and foreground lock command,
with a 300-second queue timeout. The shell EXIT trap performed the prescribed
`python3`/`shutil.rmtree` cleanup after each attempt. All five runner sessions
exited 75; the dependency installation completed. No queued or detached test
runner or background build is left running. The live switch remains off; no
live state or Git remote was touched.

DONE
