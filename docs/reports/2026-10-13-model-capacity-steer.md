# Model-capacity steer (D-CAP1), 2026-10-13

A provider's refusal of the *model* for want of capacity now moves the job's next attempt to the next model of its chain, for 15 minutes, for that job alone. Nothing about the account or the lane changes. Contract: C-9.5 (amended), with cross-references in C-4.5 and C-11.2 and a Changes entry. Base `release/217` at `f3e042953a00`.

## The incident

Codex (`gpt-6.1-sol`) ended attempts with an `error` event and then the `turn.failed` that ended the turn, both reading:

> Selected model is at capacity. Please try a different model.

The hub saw three detached `hard` jobs lose hours to it:

| Job | Attempts (minutes on the provider, lane) |
| --- | --- |
| `20261013-024327-pr198-review-r6` (review) | a1 26.4 codex-1, refused; a2 11.0 codex-1, refused; a3 Opus on claude-10, five-hour limited |
| `20261013-015056-pr181-fix8` (build) | a1 3.7 codex-1, a2 16.5 codex-1, a3 14.6 codex-2: all refused; failed after `max_attempts` with uncommitted work in its worktree |
| `hub-fix-github-125-r7-resume` (build) | `20261013-030841-…` a1 5.0 codex-1, refused; resubmitted as `20261013-040242-…`: a1 2.6 and a2 34.5 on codex-1, refused |

The brief named the first two jobs only. The third row is my reading of the job folders: the same `hard` build was refused, resubmitted, and refused twice more on one lane. The hub should correct it if it meant another job.

### What the code did

- `subfleet/adapters/codex.py` matched the text against `TRANSIENT_RE` (`capacity`) and returned `transient`, "Temporary Codex transport or capacity failure". It recorded the *first* matching signal as admission evidence. In `pr198-review-r6/a1` that was an earlier `Reconnecting... 2/5 (unexpected status 503 …)` that Codex had recovered from, not the refusal that ended the turn.
- `Daemon._retry_pin` pinned the next attempt to the same lane and model, and finalization set it 60 s out (C-4.5, C-9.5). The pinned pair was the same model, still at capacity. A second transient on that lane excluded the lane, so the third attempt went to another lane of the same model (pr181-fix8 a3 on codex-2), which refused it the same way.

## The evidence

I read every attempt folder under `~/.subfleet/jobs/202610*/` read-only: 5,507 Codex attempts. Only top-level `error` and `turn.failed` events count; model and tool text that quotes the phrase does not.

- **84 refusals** from 2026-10-01 14:45Z to 2026-10-13 09:11Z: 82 on `gpt-6.1-sol` and 2 whose launch named no model. All 84 were classed `transient`. Their attempts ran 0.5 to 356 minutes before the refusal (median 17.4).
- **The refusals are intermittent, not an outage.** Of 5,411 finished `gpt-6.1-sol` attempts, 82 were refused (1.5%). I bucketed each attempt by how long after the fleet's latest refusal it started:

| Started after the latest refusal | Attempts | Refused |
| --- | ---: | ---: |
| 0 to 5 min | 209 | 24 (11.5%) |
| 5 to 15 min | 181 | 16 (8.8%) |
| 15 to 30 min | 141 | 8 (5.7%) |
| 30 to 60 min | 153 | 5 (3.3%) |
| 60 to 120 min | 151 | 6 (4.0%) |
| 120 to 240 min | 321 | 13 (4.0%) |
| over 240 min | 4,221 | 9 (0.2%) |

  Of the 390 attempts started within 15 minutes of a refusal, 350 were not refused (338 ended `ok`).
- **The same job's next `gpt-6.1-sol` attempt after a refusal** ended `ok` 50 times, `transient` once, and was refused again 8 times. Waiting did not obviously help: within 5 minutes, 4 of 36 were refused; at 5 to 30 minutes, 3 of 15; after 30 minutes, 1 of 8. The samples are small.
- **The live policy's chains** (`~/.subfleet/policy.json`, read only): `review` is `[sol61, opus]` at `standard` and `hard`; `build` is `sol61` alone at `hard`. A refused hard review has a next model; a refused hard build has none.

## The mechanism chosen

### Candidates

1. **A model-scoped closure (`Closure` scope = model id) on each lane of the provider.** Rejected. Closures are per lane. C-11.2 refuses a closed scope to every job, including one pinned with `-m`, which the decision keeps on today's retry. Status, `lanes release`, alerts and reset-credit demand (C-23.16) would all read a lane as limited, and a credit could be spent on a model that has no account limit. The decision rules out a closure on the account or lane.
2. **The fleet-wide steer** (every unpinned job skips the model for 15 minutes after any refusal). Rejected on the data. It would have moved the 350 of 390 attempts the model was serving to their second choice, and held every hard build (no second model) for 15 minutes, to spare 40 refusals.
3. **A per-job steer, derived from the job's own attempts**, beside C-4.5's existing per-job lane exclusions (`_retry_pin`). Chosen: the smallest change that makes admission's next evaluation prefer another model for that job. It needs no schema change, no new closure reason and no in-memory state. It survives a restart because it is read from the attempt row.

### Skip while the steer is live (corrected in round 2)

Within the per-job steer, `scheduler.prepare` excludes each refused model while its steer is live. The remaining models keep chain order. Admission chooses the first admissible unsteered model, or the job waits for capacity. This includes single-model chains and reviews whose Claude lanes are all disabled. `why` names the skipped model, the capacity steer and its end. Capacity rechecks stop backing off at the earliest live steer expiry, when that model becomes eligible again. The original fallback interpretation contradicted the hub's D-CAP1 decision and was removed in round 2.


### Which jobs

`scheduler.capacity_steerable(job)` is true for a job with no `pinned_model` and no `pinned_lane` whose kind is not `turn`, `resume` or `revive`:

- A pin is the operator's choice, and a pinned job evaluates one model (C-11.2).
- A turn's failover is its conversation's (C-26.7).
- A resume or a revive continues one native session (C-12.3).

`dispatch`, `handoff` and `gate-review` qualify. A gate review always pins its model, so in practice it never steers.

### Flow

1. `CodexAdapter.classify` checks `MODEL_CAPACITY_RE` (`model[ is|was][ currently] at capacity`, or `model_at_capacity`) before the other transient texts. It reads only the message of Codex's `turn.failed` or `error` events and top-level provider stderr error lines (C-9.2); tool diagnostics and their continuation lines cannot mark capacity, after authentication, old CLI, content and quota. It returns `transient`, writes no closure, sets `evidence.model_capacity = true`, and takes the matching failure as `admission`. The detail reads "Codex model at capacity: the provider asked for a different model". A server's or gateway's "capacity" stays an ordinary transient.
2. `Daemon._finalize`, for a steerable job, records `capacity_steer = {model_id, until}`, where `until` is `MODEL_CAPACITY_STEER_S` (900 s) after the attempt's `finished_at`. It sets the retry due at once, not 60 s on. A pinned job's refusal records `model_capacity` and nothing else, and keeps the 60 s retry.
3. `Daemon._retry_pin` sets no same-lane pin after a steered attempt. The lane exclusions are unchanged: a second transient on one lane still excludes it.
4. Each admission look builds the job's route with `Daemon._route_job` → `_capacity_steer(job, previous)`: the job's own transient attempts' steers whose `until` is still ahead, read once per look. The look's early evaluation, its probe choice (C-11.4) and its reservation's check (`route_check.still_stands`, C-6.3) all see the same recorded steers. Each evaluation checks their expiry against its own clock; if expiry changes the walk before reservation, admission evaluates it again. `_reset_demand` (C-23.16) routes the same way.
5. `scheduler.prepare` excludes live steered models. `decision_reason` prefixes `<model>: at capacity for this job until <t>, skipped by capacity steer (C-9.5)`, which `why` prints. The `Decision` record has no new field.

### Against the contract

- **C-4.5:** the attempt is never success and counts toward `max_attempts`. Partial output and salvage are unchanged, and a writable job's worktree carries over exactly as for any transient retry. Only the same-lane retry and its 60 s delay are replaced, for steerable jobs.
- **C-6.3:** no new read inside the reservation transaction, and no extra evaluation. The check's differential property still holds over steered jobs (tests below).
- **C-9.x:** still `transient`, never a closure (C-9.4, C-9.6). C-9.2's evidence boundary and precedence are unchanged.
- **C-11.x:** upward-only is unchanged, since nothing is added and nothing below the tier is used. Pins evaluate one model as before. C-11.4's probe choice and `promoted_past` read the decision's own walk.

## The clock: 15 minutes

No provider reports a clock, so this is a guess, marked as one in `contracts.py`. The hazard table puts most of the excess risk in the first 15 minutes after a refusal: 11.5% and 8.8%. After that it is 3 to 6% for the rest of an episode, against 0.2% outside one. The clock bounds how long a refused job skips that model. A job whose chain has no admissible unsteered model waits through that interval:

- The next attempt starts within seconds, so any clock covers it.
- If that attempt fails within 15 minutes, the job's next try still avoids the refused model.
- After 15 minutes the model is back first in its place.

A longer clock would mostly keep hard reviews on Opus for a third attempt at a risk of about 4%. I kept the hub's 15 minutes.

## Tests

Regressions first: `5c2348290` (tests), then `22d44da` (a test correction), then the fix.

| File | What it pins |
| --- | --- |
| `tests/fixtures/codex/model-at-capacity-sol` | The incident attempt pr198-review-r6/a1. Events kept verbatim, including the recovered 503; tool transcript removed. |
| `tests/unit/test_codex_adapter.py` | The refusal from `turn.failed`, an `error` event, stderr, and the incident's order is `transient`, has no closure, and is marked, with the refusal (not the 503) as admission. Other transient texts stay unmarked; stronger verdicts keep precedence; a recovered refusal before a completed turn is `ok`. Two Hypothesis properties: any surrounding text marks the refusal, and the model's own words never mark it. |
| `tests/unit/test_capacity_steer_route.py` | Live-shaped examples. Four bounded, derandomized Hypothesis properties (40 examples each): the steer excludes live refused models; arbitrary chains and admissible lane sets choose the first unsteered candidate or wait; a differential test against each model evaluated alone; the reservation check of a steered decision equals a fresh evaluation. |
| `tests/fake/test_capacity_steer_daemon.py` | In-process finalization and admission, with the real Codex classifier on the fixture. A refused hard review runs on Opus at the next pass. With no Opus lane it waits until the steer ends. After the steer ends the model is chosen again. `-m` and lane pins and an ordinary transient keep the 60 s same-lane retry. Another job is unaffected; a hard build waits while its only model is steered; `max_attempts` still ends the job; `_capacity_steer` reads only its job's live steers. |
| `tests/e2e/test_capacity_steer_e2e.py` | The real daemon, guardian and adapters, with `tests/bin/codex` replaying the fixture (`SUBFLEET_FAKE_CODEX_SCENARIO`) while Claude answers. The steered hard review runs on Opus in under 60 s and succeeds. A `-m` job retries the same lane and model 60 s on. |

Counts and mutation results: see "Runs" below.

## Runs

All runs used the worktree's `.venv` (Python 3.12.14, `uv sync --locked --group dev`), each under its own hold of `/private/tmp/claude-501/subfleet-suites.lock`, with a per-run TMPDIR and Hypothesis directory under `getconf DARWIN_USER_TEMP_DIR`.

| Run | Code | Files | Result |
| --- | --- | --- | --- |
| Baseline | `f3e042953` | `tests/unit/test_codex_adapter.py` | 90 passed in 7.2 s |
| Fixed, first run | the fix before commit (the classifier, daemon and scheduler as committed in `12972bca`, less two later comment-only and one evidence-precheck edits) | `tests/unit/test_codex_adapter.py`, `tests/fake/test_capacity_steer_daemon.py` | 137 passed, 2 failed in 11.3 s. The two failures were the test's, corrected in `22d44da`: the "high demand" reconnect is `unknown` as a last word today, so it moved to a test of texts that are never marked. Every daemon test passed. |

From about 06:00Z to after 09:00Z on 2026-10-13 the lock was held, in turn, by other sessions' long integration runs, one past 40 minutes, with six to nine sessions queued and the host at load average 100 to 140. Each of my bounded attempts (`lockf -t` up to 565 s inside the 10-minute tool window) ended `already locked`. The runs I could not get are listed in the final report for the hub to run on the host, with the base replay tree (`git archive 22d44da`) and the mutation script used for them.

## Not changed, and follow-ups

- **Claude's own capacity refusal.** `adapters/claude.py`'s `TRANSIENT_RE` includes `overload|at capacity`, and that adapter does not set the flag. The four Claude attempts whose streams contain the Codex phrase (`20261006-164039-net-r1-review-pr150` a1 and a2, `20261010-180139-lanes-touch-port-217` a1, `20261013-003522-pr195-review-r4` a1) hold it only in tool results. I did not survey Claude's own overload errors. The flag and the daemon's steer are provider-neutral, so a Claude rule would need only the classifier.
- **A probe's capacity refusal** steers nothing: a probe is the lane's, not the job's (C-11.4).
- **"We're currently experiencing high demand, which may cause temporary errors."** classifies `unknown`, not `transient`, when it is the last word: `TRANSIENT_RE` has "temporarily", not "temporary". It appeared only as a recovered reconnect in October. Left as it is; a test pins only that it is never marked.
- **stderr lines from Codex's tool router** echo the job's own files. Round 2 excludes those diagnostics and their quoted continuation from capacity signals. Other existing classification signatures retain their precedence.

## PR #207 fix round 1

Merged the existing local `origin/release/217` at `69b119f94007` in `b20791c96`, with parents `9c8a4dde7d5b` and `69b119f94007`. No Git remote was contacted. Both D-CAP1 and #195's C-9.2 completion proof hold:

- Detached Codex success requires this attempt's `turn.completed`, exit 0, a nonblank deliverable, no `turn.failed` and no spawn error. A terminal capacity refusal remains `transient`, has no closure and records `model_capacity: true`, with or without completion and even with exit 0. A nonterminal capacity refusal without completion gets its mark before the generic incomplete-turn fallback. A recovered diagnostic followed by verified success remains `ok` and unmarked. Stronger authentication, old CLI, content and quota verdicts retain precedence when success is unproven.
- C-4.3 keeps #195's `FINALIZATION_RULE` (`b3be2b6f0`): a current receipt keeps this daemon's verdict; only an older receipt reconciles with frozen success proof. Regression cases preserve the capacity mark and steer across replay and restart, both completion states, both exit codes and both receipt generations. The current marked transient also retains its class and steer beside a proven completed stream; an older verdict can reconcile to success. Cached classification is never repeated, and later stdout cannot replace the frozen refusal.
- Both capacity fixtures remain failure streams. The capacity fake copies explicit Codex output to its frozen stream; generic fake successes keep #195's `receipt_fixture` completion proof. Both scenario lists retain the incident and cut-off fixtures.

Ran `contract_merge.py` mid-merge and reviewed C-4.3, C-4.5, C-9.2, C-9.5 and C-11.2 with `contract_wdiff.py` and by reading each clause in full.

### CI correction

`994e49a1a` fixes the failing test's setup. A standard build walks `[opus, astra]`, so steering Opus correctly produces `[astra, opus]`. C-11.2 promises this order: `policy.flatten_chain` deduplicates with an insertion-ordered dictionary, and `scheduler.prepare` constructs its steered dictionary in chain order. Production routing was already deterministic.

The outside-walk test now steers below-tier Sonnet and off-chain Terra, asserts both eligible walks and compares whole decisions. Added cases verify that an in-walk steer changes the selected model, and that multiple steers retain policy order regardless of map insertion order. The file passes all **19 tests**, including three 400-example properties.

`804545c8b` corrects the four added cached-verdict fixtures to request standard read-only work: an unmeasured hard job requires a C-11.4 probe, so the first full file run had **87 passed, 4 failed** in 1048.88 s (17 min 28 s). The corrected four cases passed separately in **3.31 s**, with 87 deselected; the 87 unchanged cases were already passing. There are **91 passing cases across the two runs**.

### Validation

CPython 3.14.7, free-threaded build; dependencies installed in this workspace with `uv sync --locked --group dev`. One foreground pytest process per shared-lock hold, each with its own fresh TMPDIR and Hypothesis directory, cleaned with the requested Python command. Queue waits were bounded at 60 s, with 180 s attempts for the critical route, corrected replay cases, module names and the final mutation attempt. Every completed hold stayed below 20 minutes; the longest was 17 min 28 s. Lock failures are unrun checks, never passing checks.

| File | Result | Test runtime |
| --- | --- | --- |
| `tests/unit/test_codex_adapter.py` | 135 passed | 42.56 s |
| `tests/unit/test_capacity_steer_route.py` | 19 passed | 38.60 s |
| `tests/fake/test_capacity_steer_daemon.py` | 33 passed | 67.00 s |
| `tests/unit/test_codex_turn_completed.py` | 41 passed; 1 existing pytest deprecation warning | 15.28 s |
| `tests/fake/test_codex_turn_completed_fake.py` | 87 passed, then 4 corrected cases passed; 91 distinct cases | 1048.88 s + 3.31 s |
| `tests/fake/test_finalization_replay.py` | 10 passed | 14.30 s |
| `tests/unit/test_empty_provider_results.py` | 334 passed | 52.70 s |
| `tests/unit/test_limit_from_model_text.py` | 95 passed | 25.84 s |
| `tests/process/test_codex_isolation.py` | 37 passed; 1 detached-grandchild test deselected | 15.96 s |
| `tests/fake/test_state_contract.py` | 37 passed | 27.07 s |
| `tests/unit/test_test_module_names.py` | 1 passed | 6.49 s |

The first mutation attempt was blocked before pytest started; that infrastructure failure was not counted as a kill. The final run completed with **10/10 mutations killed**, each by an actual failing regression. All ten rules still match the merged source, and all source files were restored (`git diff --exit-code` verified). The original tool was invoked through a temporary foreground runner that gives each mutation its own lock hold and TMPDIR, rejects infrastructure failures as mutation kills, and restores source through the tool's existing `finally`. Its E2E cases and the additional replay matrix are excluded from mutation selection: E2E starts detached processes, and the original steering regressions already cover the tool's ten mutations. The full capacity replay matrix passed in the separate 33-case baseline.

The requested files total **833 distinct passing test cases** across their successful runs.

The detached-grandchild process test and E2E files are intentionally unrun under the no-detached-process rule. The full suite and Python 3.12 were not run. `git diff --check` and Python parsing of changed modules pass.

### Local history and delivery

The shared Git metadata rejected `ORIG_HEAD.lock`, and the original `.git` pointer is read-only. Commits therefore use workspace-local `.git-local` metadata on `fix/model-capacity-steer`; the original pointer is unchanged. Use `GIT_DIR="$PWD/.git-local" GIT_WORK_TREE="$PWD"` for this history. Every commit has the requested co-author trailer. No logs, generated JSON evidence or bundles were staged.

The verified delivery bundle is at `docs/reports/2026-10-13-model-capacity-steer-r207f1.bundle`, ignored by Git, for importing this local commit history. No shared checkout, live state, shared exclude file or remote was changed.


## PR #207 fix round 2

Base: `7cda25c90856`, including the hub's #191, #194 and #199 merges. No remote was contacted. All changes are confined to the assigned workspace, with commits on `fix/model-capacity-steer` in workspace-local `.git-local` metadata; the original `.git` pointer and shared Git metadata are unchanged.

### Findings and commits

- `ac9020695`: regressions first. Replaced fallback expectations with capacity waits, added the reviewer's disabled-Claude-lanes case through steer expiry, preserved the pinned retry and terminal-attempt checks, added quoted-stderr and event-payload negatives, and covered broken lock paths and runner failure accounting. Routing properties use 40 bounded examples each.
- `2b8ea654a`: P2 scheduler finding fixed. Live steered models are excluded, chain order is retained for the remaining models, and capacity waits recheck by steer expiry. C-4.5, C-9.5, C-11.2 and the contract history now state the skip-or-wait rule.
- `4a4bb4f04`: P2 classifier finding fixed. Capacity matching reads failure-event messages and top-level provider stderr errors. A tool diagnostic preserves its context across following quoted lines, including payloads prefixed `ERROR:`; unrelated failure-event fields and tool output cannot mark capacity. Structured provider errors remain usable after a tool diagnostic.
- `0f8b600f2`: gave each Hypothesis example a fresh policy, correcting the function-scoped-fixture health-check failure (initial routing run: 21 passed, one health-check failure).
- `f32d7820e`: refreshed the expiry regression's provider reading after advancing the clock. Its initial run had 32 passes and one failure caused by the fixture becoming stale, which made admission request a probe instead of reserving the next attempt.
- `e68c66895`: P3 mutation accounting finding fixed. A kill requires pytest exit 1 and a named failure in a selected regression. Lock, collection, termination and other runner errors propagate as nonzero tool failures, with source restored. Each mutation has its own lock hold and temporary Hypothesis storage; detached E2E cases are excluded. The ten mutations include one restoring the refused-model fallback.

### Validation

Final baseline: **1,006 passed, 2 skipped** across the requested files and the new mutation-runner regressions. No requested file remains unrun. Runs use Python 3.14.7 free threaded, pytest 9.1.1 and Hypothesis 6.168.1 from the frozen lockfile, one foreground pytest process per shared-lock hold, one worker, with task-owned TMPDIR and Hypothesis storage. The first two routing attempts timed out in the lock queue without running pytest (120-second queue bounds). Subsequent requested-file attempts use a 60-second queue bound. No deep profile is used. The standalone broken-lock reproduction exited **73**, reported an infrastructure error, counted no kills and restored all source files. Syntax parsing passed for all eight changed Python files.


| File | Final result |
| --- | --- |
| `tests/unit/test_codex_adapter.py` | 145 passed |
| `tests/unit/test_capacity_steer_route.py` | 22 passed |
| `tests/fake/test_capacity_steer_daemon.py` | 33 passed |
| `tests/unit/test_capacity_steer_mutations.py` | 8 passed |
| `tests/unit/test_codex_turn_completed.py` | 41 passed |
| `tests/fake/test_codex_turn_completed_fake.py` | 91 passed |
| `tests/unit/test_provider_active_cap.py` | 50 passed |
| `tests/fake/test_provider_active_cap_fake.py` | 60 passed |
| `tests/fake/test_admission_priority.py` | 32 passed |
| `tests/fake/test_admission_visibility.py` | 43 passed |
| `tests/fake/test_routing_end_to_end.py` | 14 passed, 2 skipped |
| `tests/unit/test_empty_provider_results.py` | 334 passed |
| `tests/unit/test_limit_from_model_text.py` | 95 passed |
| `tests/fake/test_state_contract.py` | 37 passed |
| `tests/unit/test_test_module_names.py` | 1 passed |

The two routing skips require process identity from sysctl/ps, which this sandbox does not permit. The full suite, detached E2E/process suites and Python 3.12 were not run in round 2. Each baseline file had a separate hold; no hold approached 20 minutes. Initial queue failures ran no pytest and are excluded from the counts. Temporary test storage was cleaned after each hold and no `.hypothesis` directory was left.

The fixed mutation tool ran in a temporary `git archive` of `0f8b600f2`, with one foreground pytest process per hold and its own temporary Hypothesis storage. Result: **10/10 verified regression kills**, tool exit **0**, with no infrastructure errors. The driver verified that all three mutated source files were restored byte for byte and removed the archive afterward.

The restored-fallback mutation was killed by `tests/fake/test_capacity_steer_daemon.py::test_c9_5_with_no_lane_for_the_next_model_the_job_waits_until_the_steer_ends`. The other kills covered removal of the steer, removal of model exclusion, dropping route evidence, restoring a same-lane pin, restoring the 60-second delay, steering pinned jobs, keeping expired steers, missing the refusal, and taking the first signal as admission evidence. Every kill named a failing regression; queue failures and the standalone broken-lock check were not counted.

The final report commit also clarifies that each evaluation consults steer expiry at its own clock; the daemon edits in that step are comments only. `git diff --check` and syntax parsing passed. No logs, JSON evidence or bundles were staged. A portable bundle of this local history is stored at `.git-local/r207f2.bundle` and verified for import against `7cda25c90856`. No shared checkout, live state or remote was changed.


## PR #207 fix round 3

Base: `930b7acff53a79d206a370251667afd8182b68f3`, including #209. The assigned workspace started clean. No remote was contacted, and no shared checkout, live store or live process was touched. Commits use workspace-local `.git-local` metadata on `fix/model-capacity-steer`, because the shared Git metadata is outside the sandbox's writable roots. The original `.git` pointer is unchanged. Use `GIT_DIR="$PWD/.git-local" GIT_WORK_TREE="$PWD"` to inspect this history.

### Findings

1. **P2, steered waiter demand — fixed.** Admission reads the job's recorded steer before C-6.9 hold-back, including a backed-off waiter. `scheduler.demand_models` excludes live steered models. A later unpinned review's full chain can still overlap the waiter's remaining model, so this particular comparison is deferred until model selection and enforced again before reservation. A later Astra attempt can pass a review steered away from Astra; an Opus selection still waits behind that review. Other FIFO comparisons, provider caps, family scope, lease order and the last slot kept for an older waiter remain intact. Demand returns at expiry. Review and single-model build regressions cover expiry priority and unchanged-verdict accounting (C-6.10).
2. **P2, captured stderr inside error messages — fixed.** C-9.5 now defines the accepted message grammar. After optional whitespace and `ERROR:`, the message must itself begin with the model-capacity refusal, optionally prefixed `Selected ` or `The `, or with `error code: model_at_capacity`. Trailing provider details are allowed. A phrase after another prefix, including a tool failure or captured-output prefix, is excluded. Authentication, old-CLI, content, quota, verified completion and terminal-failure precedence are preserved.
3. **P2, diagnostic suppresses later provider refusal — fixed.** Each Codex tracing record resets its diagnostic context. A tool diagnostic's quoted continuation, including an `ERROR:` payload, remains excluded; a subsequent independent non-tool record and its refusal are judged independently. Structured failure events remain independent of stderr context.

The earlier steer wait, expiry, pinned retry, quoted-router and verified mutation-accounting fixes are retained. The #209 completion and frozen-stream guarantees are covered again by the requested completion and frozen-stream files.

### Tests first and properties

Seven regression cases reproducing the reviewed message and queue shapes were run against baseline source copied inside `.git-local/baseline`: **all seven failed at the intended assertions** (five classifier cases and review/build hold-back), with no provider-source changes from `930b7acff`. The first baseline attempt timed out in the lock queue. A subsequent collection error exposed an incorrectly selected test-edit block; the cyber-refusal property was restored, the intended capacity property was corrected, and that error is excluded from regression counts.

The queue property uses **40 derandomized examples**, fresh stores, mixed review/build queues, both Astra and Opus targets, and pinned or unpinned later jobs. It checks that an older job's live steer cannot alone hold back a later attempt selected on that model. The classification property uses **60 derandomized examples** of interleaved provider events, independent stderr records, tool diagnostics and quoted payloads; the marker is present exactly when a provider record itself is a refusal. Message-boundary examples test both sides of C-9.5's grammar. Both properties disable the example database and deadline.

The #199 state machine now generates model-capacity refusals through real finalization. Its independent demand oracle removes only live recorded steers, checks the actual selected model for the narrow steering exception, and tests fair progress after the steer ends. Both the ordinary and provider-cap variants use the **CI profile: 40 examples of at most 30 steps**. #199's non-interference, progress and recheck properties and #201's provider admission behavior are checked alongside the new regressions.


### Validation discipline

Runs use Python 3.14.7 free threaded, pytest 9.1.1 and Hypothesis 6.168.1 from `uv sync --frozen --group dev`. Every file runs in its own foreground shared-lock hold, one worker, with a task-owned DARWIN_USER_TEMP_DIR temporary directory and Hypothesis storage. No deep profile is used. Queue timeouts run no pytest and are excluded from case counts. A longer queue wait is separate from the duration of a hold; no test hold approached 20 minutes. Temporary test storage is removed after each attempt.

Intermediate failures were corrected in separate commits: the initial wrong property-edit block; the remaining-chain FIFO overlap found by the review regression; and an Opus FIFO safety fixture that accidentally made its older job permanently unadmittable under C-11.8. Editing the temporary test wrapper while a batch was active also caused a parse error after its last file; the wrapper was fixed, and affected outstanding checks were retried. These are excluded from final passing counts.

The first mutation campaign attempt exited **75**, recorded **zero kills**, and verified all three source files restored byte for byte after failing to acquire the shared lock. The committed tool now accepts `SUBFLEET_SUITE_LOCK_WAIT_S` (60-second default) so a foreground campaign may wait longer in the queue. The criterion for a kill remains pytest exit 1 plus a named failed regression in a selected file; infrastructure errors remain errors.

### Commits

- `4b6bf5662`: test: reproduce round-3 capacity steering failures.
- `6a770e715`: fix: exclude live capacity steers from FIFO model demand.
- `6ad90f2b1`: fix: classify capacity refusals within provider message records.
- `703ac34a9`: test: scope the capacity message property to provider refusals.
- `4b9c0c4be`: test: restore each round-3 bug in the capacity mutation campaign.
- `f1b823ecc`: test: check FIFO non-interference for either steered model.
- `90eccd0c3`: test: preserve FIFO on a steered waiter remaining model.
- `e58721380`: test: judge steered waiter competition on the selected model.
- `b9140e3e8`: fix: compare steered FIFO waiters against the selected model.
- `aa092db2d`: test: keep the remaining-model FIFO waiter admissible later.
- `36c479f31`: test: make the mutation lock queue wait configurable.

### Passing runs

**1,152 passed, 2 skipped, 3 expected failures** across 19 distinct files. Every file had its own shared-lock hold.

| File | Passed | Other |
| --- | ---: | --- |
| `tests/fake/test_admission_priority.py` | 32 | — |
| `tests/fake/test_admission_properties.py` | 14 | 3 expected failures |
| `tests/fake/test_admission_stateful.py` | 2 | — |
| `tests/fake/test_admission_visibility.py` | 43 | — |
| `tests/fake/test_capacity_steer_daemon.py` | 38 | — |
| `tests/fake/test_codex_turn_completed_fake.py` | 109 | — |
| `tests/fake/test_frozen_stream_nonregular.py` | 32 | — |
| `tests/fake/test_provider_active_cap_fake.py` | 60 | — |
| `tests/fake/test_routing_end_to_end.py` | 14 | 2 skipped |
| `tests/fake/test_state_contract.py` | 37 | — |
| `tests/unit/test_capacity_steer_mutations.py` | 8 | — |
| `tests/unit/test_capacity_steer_route.py` | 22 | — |
| `tests/unit/test_codex_adapter.py` | 172 | — |
| `tests/unit/test_codex_turn_completed.py` | 41 | — |
| `tests/unit/test_empty_provider_results.py` | 334 | — |
| `tests/unit/test_frozen_provider_streams.py` | 48 | — |
| `tests/unit/test_limit_from_model_text.py` | 95 | — |
| `tests/unit/test_provider_active_cap.py` | 50 | — |
| `tests/unit/test_test_module_names.py` | 1 | — |

The two routing skips require process identity unavailable in this sandbox. The three expected failures are pre-existing admission-property cases. The full suite, detached E2E/process suites and Python 3.12 were not run.

### Mutation results

The final campaign ran committed `36c479f31` in a workspace-owned archive, with `SUBFLEET_SUITE_LOCK_WAIT_S=600` for the lock queue. Result: **14/14 verified named regression kills**, tool exit **0**, with no infrastructure errors in that campaign. Each mutation had its own foreground shared-lock hold and bounded Hypothesis storage. The driver verified that the daemon, scheduler and Codex adapter were restored byte for byte afterward.

| Restored round-3 bug | Regression that killed it |
| --- | --- |
| Refused model retained in FIFO demand | `test_r3_steered_waiter_does_not_hold_back_later_jobs_for_the_refused_model[review]` |
| Remaining-chain overlap blocks a later Astra review | `test_r3_steered_waiter_does_not_hold_back_later_jobs_for_the_refused_model[review]` |
| Captured stderr inside an error message marks capacity | `test_r3_capacity_quoted_inside_failure_messages_is_not_a_refusal[Tool execution failed with exit code 1. Captured stderr:\n-error]` |
| A tool diagnostic suppresses all later provider records | `test_r3_independent_provider_record_after_tool_diagnostic_marks_capacity` |

The ten earlier mutations also died: removal of the steer, removal of model exclusion, omitted route evidence, the same-lane pin, the 60-second delay, steering pinned jobs, retaining expired steers, restoring refused-model fallback, missing the classifier marker, and taking the first signal as admission evidence. No queue failure or collection error was counted as a kill.

### Delivery and cleanup

No requested file remains unrun. `git diff --check` and syntax parsing of all changed Python modules pass. Every commit has `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. The final documentation step also corrects the stale duration comment to describe exclusion while the steer is live; the 900-second value is unchanged.

Commits remain on `fix/model-capacity-steer` in workspace-local `.git-local` metadata. The verified portable bundle is `.git-local/r207f3.bundle`, for import against `930b7acff53a`. No logs, JSON evidence, bundles or temporary archives were staged. Test temporary storage and replay archives were cleaned, and no `.hypothesis` directory remains. No push, PR comment or remote operation was performed.


## PR #207 fix round 4

Round start: `59022aba5`. Resumed after the cold boot on 2026-10-15 at recovered
head `c68d56f4e356`.
The assigned checkout was clean. The four recovered commits were read in order;
their completed work was preserved:

- `221102602`: the review-r3b minimized input is a named regression: three
  unpinned standard reviews, two Codex lanes, one active-attempt slot and an
  `os-error` probe. It requires all three jobs to succeed within seven passes.
- `3c41ac38a`: deterministic regressions run inside a bounded Hypothesis context,
  allowing the state machine's notes and events.
- `9fc0fd66e`: simulated successes write a frozen Codex `turn.completed` or Claude
  success `result`, alongside the exit-zero receipt and nonblank deliverable
  required by C-9.2. Unsuccessful finishes write no success terminal event and
  retain nonzero exit receipts. Eight cases check both providers' success,
  transient, quota and model-capacity outcomes through real finalization.
- `c68d56f4e`: explicit CLI seeds select distinct state-machine searches while
  retaining the CI bounds of 40 examples and at most 30 steps per machine.

The recovered fixture matches the finalizer's completion requirements; no source
step needed repeating. Each `tests/fake/test_admission_stateful.py` invocation
uses the full CI bounds, with no example-count or shrinking overrides, and
includes both state machines and all nine explicit regression cases.

| CI seed | Result | Pytest runtime |
| --- | --- | --- |
| `207401` | 11 passed | 82.50 s |
| `207402` | 11 passed | 45.75 s |
| `207403` | 11 passed | 95.38 s |

All three required CI seeds passed; no new admission bug was found.

| Requested file | Result |
| --- | --- |
| `tests/fake/test_admission_stateful.py` | 11 passed per seed; three seeds |
| `tests/fake/test_admission_properties.py` | 27 passed, 1 expected failure |
| `tests/fake/test_admission_held_waiters.py` | 1 passed |
| `tests/unit/test_held_waiter_holdback.py` | 6 passed |
| `tests/fake/test_capacity_steer_daemon.py` | 38 passed |
| `tests/unit/test_capacity_steer_route.py` | 22 passed |
| `tests/unit/test_codex_adapter.py` | 172 passed |
| `tests/fake/test_admission_priority.py` | 32 passed |
| `tests/fake/test_routing_end_to_end.py` | 14 passed, 2 skipped |
| `tests/fake/test_state_contract.py` | 37 passed |

The first file-group hold finished with **34 passed, 1 expected failure in
413.55 s**. The expected failure is the existing C-6.10 wait-kind/unchanged-verdict
case. The capacity-steering/adapter group finished with **232 passed in
100.89 s**. The priority/routing/state group finished with **83 passed, 2 skipped
in 72.56 s**. Both skips require unavailable macOS sysctl/ps process inspection.

Total: **382 passing executions, 2 skipped, 1 expected failure**, including the
three stateful runs; **360 distinct passing cases**. No requested file remains
unrun, and no additional production-code fix was needed.

All six invocations ran in the foreground, one pytest process and one worker at
a time, each under its own `lockf -k` shared-lock hold. Queue waits had no timeout;
every hold stayed below 20 minutes. All pytest runtimes were below seven minutes.
The frozen environment is Python 3.14.7 free threaded, pytest 9.1.1 and Hypothesis
6.168.1. `HYPOTHESIS_PROFILE=ci` was set throughout; other properties retained
their committed bounded settings, and no deep profile was used. Each run had
its own task-owned TMPDIR and Hypothesis storage, removed with the requested
`shutil.rmtree` cleanup after pytest exited. No `.hypothesis` directory or test
wrapper remains. `git diff --check` and fixture syntax parsing passed.

The shared Git metadata is outside the writable roots, so new commits use
workspace-local `.git-local` metadata on `fix/model-capacity-steer`, with the
original `.git` pointer unchanged. Inspect it with
`GIT_DIR="$PWD/.git-local" GIT_WORK_TREE="$PWD" git log`. No Git remote, caller
checkout, live store or live process was accessed; no sub-agents were used.
The portable history is `.git-local/r207f4.bundle`, verified against round start
`59022aba5`. Every commit retains the requested co-author trailer. Only the named
report file was staged after the restart; no logs, JSON evidence or bundles were
staged. Nothing was pushed and no PR comment was sent.
