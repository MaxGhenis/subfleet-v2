# D-ADM3: obsolete admission predecessors on release/217

Base: `d72597b5c9e2840c4405038a67366484352dbf5a` (release/217).
Assigned branch: `fix/admission-adm3-217`. Work stayed in the assigned workspace;
no push and no sub-agents. The caller already has this branch checked out and the
shared Git metadata is outside the writable workspace. Commits therefore use
workspace-local `.git-local` metadata (`git --git-dir=.git-local --work-tree=.`),
on the assigned branch, with the base's objects available through an alternate.

## Reproduction before changing the daemon

The unseeded `HYPOTHESIS_PROFILE=dev` provider-cap machine passed (16.20 s).
Recorded dev seeds 1–5 passed in 3.79, 21.98, 22.55, 9.43 and 5.70 s.
Seed 6 failed after full shrinking in 273.91 s with Hypothesis 6.168.1.
Every search retained dev's 40 examples and 30 steps, no example database and
shrinking enabled. No `deep` search was run.

Hypothesis's minimized sequence (all jobs unpinned, without exclusions or
authorizations; absent model/task becomes `review` on submission):

```python
state = ProviderCapMachine()
review = dict(model=None, task=None, pin=None, legacy=False,
              authorize=False, exclusions="[]")
state.fleet(
    cap=None, per_lane=1, reserve=False, claude=[None], codex_email=None,
    second_codex=False, capacity=[],
    queue=[dict(review, tier=tier) for tier in
           ("standard", "standard", "hard", "standard", "hard")],
    probe="ok", prove=None, provider_caps={"claude": 1},
)
state.admission_pass()
state.cancel(job=0, tick=True)
state.teardown()
```

The first two standard reviews start, on Claude and Codex respectively. The
older hard review waits `no-slot`; the third standard review waits `provider-cap`;
the younger hard review waits `behind-older-job` naming the older hard review.
All three have a recheck at virtual t=1. Cancellation selects the older hard
review (the first remaining job in submission order) and runs another pass at
t=0. The younger hard review repeats its cached hold naming that cancelled job.
P2 fails: `review-4 is held behind review-2, which is no longer pending`.
This is another manifestation of the obsolete-predecessor defect, captured by
`test_adm3_cancelled_job_does_not_remain_an_older_waiter`.

An additional pre-fix dev run, seed 7, found two fully shrunk failures in
306.78 s, both on that same path. Their common sequence is:

```python
state = ProviderCapMachine()
state.fleet(cap=None, per_lane=None, reserve=False, claude=[], codex_email=None,
            second_codex=False, capacity=[], queue=[], probe="ok", prove=None,
            provider_caps={"codex": 1})
state.submit(jobs=[dict(review, tier="standard"), dict(review, tier="standard"),
                   dict(review, tier="standard", task="sweep")], tick=False)
state.admission_pass()
# One of these two steps, each independently shrunk:
state.cancel(job=0, tick=False)
# OR: state.capacity_changes(change=("toggle", 0, False), tick=False)
state.admission_pass()
state.teardown()
```

The first review runs; the second waits `provider-cap`; the sweep waits behind
the second. In the cancellation case the sweep names a cancelled predecessor.
In the disabled-lane case it names a predecessor now refused `for_good`
(`no-lanes`, `disabled`), which holds nobody back under C-11.8. Both variants are
explicit regressions in `test_adm3_obsolete_provider_waiter_is_rechecked`.

The focused D-ADM3 start regression uses two standard reviews on one Claude
lane: an older Opus pin and a younger Sonnet pin, with Opus closed until t=5.
Run admission at t=0, 1 and 3. Both waits back off, but the older job's known
reset bounds its next check to t=5, while the younger's clock runs until t=7.
Admission at t=5 starts the Opus review, then must immediately recheck the
Sonnet review. With a provider cap of two both start in that same pass; with a
cap of one the younger must wait `provider-cap`, never behind the running job.
`test_adm3_started_job_does_not_remain_an_older_waiter[1/2]` covers both counts.

## Cause and contract judgment

The daemon's backed-off capacity path copies the last look's full hold without
checking whether its `behind-older-job` predecessor still belongs to the pass's
waiters. A predecessor that starts, ends, or otherwise stops holding capacity
can therefore remain in a later job's hold. The live provider count alone does
not justify that historical FIFO relationship.

The code is wrong; the non-interference invariant stays intact. C-6.9 grants
hold-back only to an older capacity waiter, and C-6.4a applies that order among
peers sharing the selected provider count. A running attempt still consumes
that count but is no longer a waiter. C-6.10's backoff and C-6.11's full-hold
repetition need an explicit exception when the predecessor stops holding back.
C-9.5's transient retry/model steer rules do not authorize such a hold (the
shrunk case has neither retries nor steers). C-9.6's expiring model closure and
C-11.2's model routing permit admission once the closure ends and count permits;
C-11.8's standing-refusal exceptions do not apply to these temporary waits.

## Fix and validation

Rule: a backed-off `behind-older-job` hold is reused only while its named
predecessor remains a capacity waiter in the current pass; otherwise recheck
the younger job immediately, with all current caps and older waiters applying.

`Daemon._admit_pass` checks the named predecessor against its existing waiter
list, excluding lease waiters. An obsolete relationship brings the capacity
look forward just like a freed lease or a probe turn becoming available. It
does not change routing, provider counts, the reserving transaction, or the
invariant. The amendment and change-log entry cover C-6.9, C-6.10 and C-6.11.

- `878075a2e`: tests first, five deterministic regression variants.
- `6795918bc`: daemon fix and contract amendment.
- `10d5367a7`: additional bounded CI provider-cap seed, measured below 10 s.

On the unchanged base daemon all five variants failed at their intended
assertions (`5 failed, 11 deselected`, 2.78 s). With the fix, all five passed
(`5 passed, 11 deselected`, 0.83 s).

Mutation: replaced `subfleet/daemon.py` with its pre-fix version from
`878075a2e` and selected
`tests/fake/test_admission_stateful.py::test_adm3_started_job_does_not_remain_an_older_waiter[2]`.
It failed at the intended assertion (younger review `waiting`, expected
`running`; its hold named the running older review), `1 failed` in 1.31 s.
Restored the committed daemon and verified its diff was empty before the full
suite runs. No mutant is left in the workspace.

The full stateful file passed under `ci`: 16 tests, 27.84 s pytest elapsed,
28.46 s holding the shared lock (175.31 s including the lock wait).

## Test execution

All pytest processes ran serially in foreground runners, each under its own
`/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock` hold, with no
lock-wait timeout and no parallel pytest workers. `SUBFLEET_RETENTION_DORMANT` and the installed
release's inherited `PYTHONPATH` were unset. The default uv cache was not
writable, so uv used `UV_CACHE_DIR` under the job's scratch directory. Hypothesis
storage and pytest temporary files were isolated there too. The interpreter was
CPython 3.14.7 free-threaded; dependencies came from the committed `uv.lock`.

Each normal search retains `max_examples=40` and `stateful_step_count=30`.
The `dev` sweep seeds are 1, 2, 3, 6 and 7, including the original failing seeds, and run both state
machines (the only other machine is `TestAdmissionPass`).

| File/run | Result | Pytest elapsed | Lock hold |
| --- | --- | --- | --- |
| `tests/fake/test_admission_stateful.py`, ci | 16 passed | 27.84 s | 28.46 s |
| same file, dev seed 1 | 16 passed | 24.37 s | 25.16 s |
| same file, dev seed 2 | 16 passed | 37.09 s | 37.73 s |
| same file, dev seed 3 | 16 passed | 31.66 s | 37.38 s |
| same file, dev seed 6 | 16 passed | 28.72 s | 29.12 s |
| same file, dev seed 7 | 16 passed | 15.77 s | 16.22 s |

The five-seed sweep passed both machines and all explicit cases: 80 test
executions. No other failure was found in `TestAdmissionPass` or in the
provider-cap machine after the fix. Seeds 6 and 7 no longer shrink a failure.

## Additional CI coverage

Measured `HYPOTHESIS_PROFILE=ci` with `--hypothesis-seed=1` on
`tests/fake/test_admission_stateful.py::TestAdmissionPassProviderCaps`: one test
passed in 4.36 s (4.04 s call). A timestamp written immediately after acquiring
the shared lock measured 7.82 s through command completion, including uv,
pytest startup and shutdown; the 196.00 s including queue time is not execution
cost. The execution cost is below 10 s, so CI now runs this class with seed 1
once per interpreter on shard 1. Its existing explicit-seed fixture honors the
CLI seed while keeping ci's bounds and disabled database. The default CI
search and seed 314159 remain available, and the deterministic ADM3 cases run
in the ordinary file selection.

## Neighboring suites

| File | Passed |
| --- | ---: |
| `tests/fake/test_capacity_steer_daemon.py` | 54 |
| `tests/unit/test_capacity_steer_route.py` | 22 |
| `tests/unit/test_capacity_steer_mutations.py` | 10 |
| `tests/fake/test_unadmittable_pin_admission.py` | 24 |
| `tests/fake/test_unadmittable_pin_review.py` | 7 |
| `tests/fake/test_unadmittable_pin_rereview.py` | 6 |
| `tests/unit/test_scheduler.py` | 97 |
| `tests/fake/test_admission_route_isolation.py` | 93 |
| `tests/fake/test_state_contract.py` | 37 |
| `tests/unit/test_test_module_names.py` | 1 |

The capacity/pin group ran in one hold: 123 passed, 45.35 s pytest elapsed,
45.99 s holding the lock. The requested `tests/fake/test_unadmittable_pin.py`
does not exist here; the three existing pin files above were run instead.
The scheduler/isolation group ran in one hold: 190 passed, 29.46 s pytest
elapsed, 29.90 s holding the lock.
The state/module-contract group ran in one hold: 38 passed, 3.35 s pytest
elapsed, 3.75 s holding the lock.

## Completion

All existing requested suites passed, with no skipped or xfailed tests. The
final matrix totals 448 passing test executions (96 across the six full
stateful-file runs, one additional CI class run, and 351 neighboring tests),
in addition to the earlier five dedicated regression passes. The expected
base and mutation failures were checked as named test failures, not runner
errors. Every hold completed well within 20 minutes; the longest search was
the 306.78 s pre-fix shrink. No test was left running and no hold was retained.

Unrun: the exact requested `tests/fake/test_unadmittable_pin.py` path is absent;
its three existing counterparts passed above. No other requested check is
unrun. No deep profile, live daemon, live store, network remote, or sub-agent
was used. Scratch, its uv cache, logs, XML evidence and Hypothesis storage were
removed after validation; no workspace `.hypothesis` directory remains.
The report is the final documentation step; no evidence files are staged.
