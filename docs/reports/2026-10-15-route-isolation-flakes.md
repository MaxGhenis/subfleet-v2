# Admission route isolation flakes

Base: `2012bbed4926` (`release/217`). No production code or shared fixtures changed.

## Store-lock waiter

`test_c6_3_a_long_wait_for_the_store_lock_does_not_void_the_decision`
released the store lock before its holder thread set `done`. Admission could
acquire the lock, finish, and assert `done.is_set()` before the holder ran again.

Reproduction: a scratch copy gates the holder between leaving its transaction
and setting `done`. Joining the holder releases the gate; a `finally` join also
cleans up after a failure. This forces admission to overtake the signal and the
original assertion fails deterministically. The fixed test retains the thread
and joins it in `finally` before checking completion. It captures admission elapsed time before
the join, so the >=1.5-second check still proves admission waited for the lock.
The same gate passes with the fix.

## Younger job and transient retry

The cited CI revision, `6e604df68`, has an older test body than the assigned base:
it does **not** hold the clock. The missing key ending in `-opus` belongs to the
younger job. The failing lookup is `service._holds[younger]` after the second
admission pass, not `service._retry_verdicts[retry]` after the first. The retry is
submitted with task `review`, and its verdict remains present.

Reproduction: copy that historical test body into a scratch test, apply the
current capped-policy fixture, and control daemon/scheduler clocks. Advance the
clock to one second after the recorded retry deadline before the second pass.
Both jobs are reserved in FIFO order, the retry verdict is still false, and the
historical assertion raises `KeyError` for the younger `-opus` job because it
was admitted and has no hold. This models a loaded runner spending the retry's
one-second interval between passes; no competing admission thread is needed.

The assigned base already contains the fix from `09f82e1ca`: held daemon and
scheduler clocks and explicit before/due/after checks. Those cases verify a hold
only before the deadline and verify FIFO reservation once due. This change adds
prompt/loaded-runner cases for each clock position, spending the retry interval
on the real clock before the younger submission. A scratch run also holds the
module clock three seconds behind the real clock to check clock separation.
There is no production-clock fix in this change: the historical failure was the
test assuming the second pass would run before the retry became due.

## File scan

- The store-lock test is the only Event set after unlock and immediately asserted
  after a main-thread call. The other thread helpers already join their threads.
- The two `HeldClock` users are the younger-job test and
  `test_c6_9_a_retry_that_lets_its_pin_go_keeps_its_place_behind_older_jobs`.
  The latter already tests a real-time stall. Their direct `datetime.now()` calls
  initialize the held clock before patching; subsequent deadline calculations
  and admission decisions use the held clock. No additional fix was needed.
- Reading-age and override-expiry tests use real clocks intentionally and do not
  run under `HeldClock`.

## Validation

Verified on 2026-10-16. The forced checks ran on Python 3.14.7 free-threaded.
Required suites ran on
Python 3.12.14, matching the CI minor version. `pytest-randomly` is not installed;
full-file run 1 explicitly disables it anyway. Every pytest runs alone in a
separate hold of the shared suite lock, with the CI Hypothesis profile and
scratch storage/cache. No push.

| Run | Result | Pytest duration |
| --- | --- | --- |
| Before: original lock assertion + historical retry assertion + existing held-clock cases | 2 expected failures, 3 passed, 86 deselected | 32.16 s |
| After: fixed lock assertion + expanded held-clock cases, same gate and real-clock offset | 7 passed, 87 deselected | 11.36 s |
| Full route file 1 (`-p no:randomly`) | 93 passed | 89.52 s |
| Full route file 2 | 93 passed | 57.45 s |
| Full route file 3 | 93 passed | 105.77 s |
| State contract + test module names | 38 passed (37 state, 1 module-name) | 18.80 s |

No production code or shared fixtures changed, so the conditional run of the
other admission files does not apply.

Implementation commits: `6eaea29e1` (join holder), `0ba513c43` (held-clock load cases).

Commits use workspace-local `.git-local` metadata because the linked worktree's
shared Git metadata is outside the writable workspace.

The unstaged delivery bundle `docs/reports/2026-10-15-route-isolation-flakes.bundle`
contains the three commits and requires base `2012bbed4926`.
