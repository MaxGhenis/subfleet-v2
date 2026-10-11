# PR #180 rollback fix, review R1

Base: `cacdb35aad7c`. Early implementation head: `8c659e2c4`. Implementation and this provisional report committed before waiting for the shared suites lock. Shared Git metadata lies outside the writable workspace; commits use `.git-local` on `feat/admission-ramp`, preserving the original base and history. No remote or sub-agent is used.

Changes:
- `subfleet/daemon.py`: re-read detached reservation rows with the reserving transaction’s reader and the timestamp to be inserted. Recheck before any lease is taken; a spent budget sets the `ramp` hold and holds later ordinary jobs in the pass. Disabled policy and attended work remain outside all ramp reads.
- `subfleet/ramp.py`: count all starts later than the cutoff, future timestamps included, and retain supplied census history through the pass. Restart uses the same cutoff-only accounting.
- `tests/fake/test_admission_ramp.py`: three actual-daemon rollback regressions (next pass, actual restart, workspace rollback between check and commit), plus an independent commit-order/elapsed-time Hypothesis property. Check no leases, disk reservation or capacity backoff for the rejected placement. Verify immediate snapshot accounting at each commit.
- `tests/unit/test_placement_ramp.py`: the fourth review regression, expired census history restored by a backwards look; arbitrary clock sequence property and updated conservative future-row expectations.
- `docs/acceptance-contract.md:233`: C-6.18 specifies cutoff-only counting, transaction rechecks, restart behavior and backward-clock safety. Real elapsed bounds require that positive wall advances consume elapsed time; arbitrary forward jumps cannot be bounded by a UTC-only ledger. Backwards corrections may delay liveness.

The supplied `review-r1-evidence/` directory was empty; all four reproductions were reconstructed from the full report. No existing uncommitted changes were present. GitNexus tools were unavailable; source tracing confirmed the admission path.

Validation pending: four regressions on base and fixed source, permitted suites with Hypothesis statistics, and four mutation checks (own-pass count, attended count, reconstruction, process-memory only). Every pytest process will run under the required suites lock, with a job-specific TMPDIR removed afterwards. No background runner will be left.

A serial replay tool, `tools/check_placement_ramp_r1.py`, restores production source in `finally`, records SHA-256 restoration checks and removes each TMPDIR. Baseline replay queued under the shared lock. Environment: CPython 3.14.7 free-threaded, pytest 9.1.1, Hypothesis 6.168.1, installed from `uv.lock`. New properties request 120 unit examples and 30 daemon examples, each with one explicit rollback example; actual counts pending.

Baseline replay: **4 failed, 0 passed in 28.42 s**, exactly the four requested rollback regressions. Production source restored by SHA-256 comparison; TMPDIR removed. Evidence: `2026-10-11-placement-ramp-r1-baseline.json`. The lease acquisition and disabled-policy reservation timestamp calls retain their original order; only enabled detached ramp checks bind the timestamp before their durable budget check.

## C-6.18 text changed

A non-priority detached placement of any accepted non-turn job kind (`dispatch`, `resume`, `revive`, `gate-review`, or `handoff`), including a retry, commits only while the number of detached non-probe attempts whose `reserved_at` is later than `now - window_s` is below `K`. This includes future-dated reservations: a backwards wall-clock step can only make the ramp stricter, never grant a fresh burst, including after restart. The reserving transaction rechecks this budget against durable rows at the exact `reserved_at` it will insert, before taking leases; a spent budget holds that job and later ordinary jobs in the pass with the `ramp` reason and takes no placement resources. Each enabled detached pass reconstructs from attempt rows and adds its own committed placements immediately; reservation checks reconstruct again because a clock rollback during workspace preparation can revive rows that expired at pass entry. Completed, failed and recovered reservations count too. Judged in commit order, every real elapsed window of `window_s` seconds contains at most `K` non-priority starts, including backwards clock corrections, provided positive wall-clock advances take at least that much elapsed time (forward clock jumps are outside a wall-clock ledger’s guarantee).

With free capacity, no continuing priority arrivals and a wall clock that eventually advances, every unchanged eligible finite queue eventually places in admission order. The following time bounds assume no backwards clock corrections; such corrections can delay expiry.

## Fixed validation

On fixed production head `c587a277d`: **423 passed, 0 failed, 0 skipped in 419.37 s (6:59)**, in one locked pytest process. All four rollback regressions passed. Run files: `tests/fake/test_admission_ramp.py`, `tests/unit/test_placement_ramp.py`, `tests/fake/test_admission_disk.py`, `tests/fake/test_admission_lease_waiters.py`, all five `tests/unit/test_policy*.py`, `tests/fake/test_policy_sandbox.py`, and `tests/unit/test_test_module_names.py`. Lock hold was below 20 minutes; TMPDIR removed. Full output and command: `2026-10-11-placement-ramp-r1-suite.json`.

Hypothesis generate-phase counts (explicit examples run separately):

| Property | Passing generated | Invalid | Explicit |
| --- | ---: | ---: | ---: |
| New unit arbitrary clocks / commit order / real elapsed R1 | 120 | 11 | 1 |
| New real daemon check/commit clocks and restarts R1 | 30 | 4 | 1 |
| Existing mixed queues, gates and restarts R1/R3/R5 | 120 | 32 | 0 |
| Existing free-capacity R2 | 120 | 26 | 0 |
| Existing priority/FIFO R4 | 120 | 29 | 0 |
| Existing restart allowance R5 | 120 | 0 | 0 |
| Enabled daemon ordering/liveness/restart | 20 | 6 | 0 |
| Disabled/absent frozen admission differential R6 | 30 | 0 | 1 |
| Lease-waiter differential | 30 | 6 | 0 |
| Policy whole-allowance | 150 | 0 | 0 |
| Policy initial-window R2 | 150 | 18 | 0 |
| Valid tier mixes | 250 | 38 | 0 |
| Invalid tier lists | 80 | 10 | 0 |

Mutation replay pending. No suite outside the permitted file list was run; `test_admission_ramp_retention` and separate `test_disk*` modules were intentionally unrun under this round’s scope.
