# PR #180 placement-ramp rollback fix (R1)

Fixed both P2s from base `cacdb35aad7c`. **423 passed, zero failed or skipped**, including all four rollback regressions. All four mutations killed. Production source is byte-identical to tested head `c587a277d`; the final documentation clarifies that a rejected placement takes no new leases or disk reservation.

Implementation and a provisional report were committed as `8c659e2c4` before waiting for the shared suites lock. Commits are on `feat/admission-ramp` in `.git-local`, because shared Git metadata is outside the writable workspace. All commits retain their ancestry and the requested co-author trailer. No network remote, caller checkout write, or sub-agent was used.

Changes:

- `subfleet/daemon.py:3054` / `:3065`: let the reserving transaction supply the ledger reader and timestamp, retaining the same cutoff and reconstruction path used at startup. Disabled policy reads no ramp history or clock.
- `subfleet/daemon.py:5585`: reconstruct and recheck the ramp under the reservation write lock, at the exact timestamp to be inserted, before acquiring leases. A spent budget holds the job and later ordinary jobs with `ramp`; no disk reservation or capacity backoff is added. Disabled/attended clock-call order is preserved.
- `subfleet/ramp.py:63` / `:106`: retain every supplied census row for this pass and count every reservation later than the cutoff, future-dated rows included. Retention’s existing future-row pinning already agrees.
- `tests/fake/test_admission_ramp.py:215`: three actual-daemon rollback regressions; `:527`: generated check/commit clocks, commit-order real elapsed windows, and actual restarts. `:181` verifies immediate accounting at each committed placement and reads once at pass entry plus at each commit.
- `tests/unit/test_placement_ramp.py:141`: census history regression; `:225`: independent elapsed-time and commit-prefix oracle for arbitrary clock sequences and restarts. Existing future-row expectations now require conservative accounting.
- `docs/acceptance-contract.md:233`: C-6.18 text below. `tools/check_placement_ramp_r1.py:1`: serial, locked baseline and mutation replay, source restoration and TMPDIR cleanup.

The supplied `review-r1-evidence/` directory was empty; the four reproductions were reconstructed from the full review.

| Rollback regression | Base `cacdb35a` | Fixed |
| --- | --- | --- |
| Start at 100; next pass at 90 | Failed: second start committed | Passed; waits until 160 |
| Start at 100; actual daemon restart at 90 | Failed: second start committed | Passed; future durable row counts |
| Start at 100; check at 160; workspace preparation at 150 | Failed: second start committed | Passed; no new leases/budget/backoff; later job held |
| Census at 160; look at 150 revives the 100 row | Failed: row lost | Passed; history retained |

Baseline: **4 failed in 28.42 s**, exactly the requested cases. Fixed suites: **423 passed in 419.37 s (6:59)**. Files: `tests/fake/test_admission_ramp.py`, `tests/unit/test_placement_ramp.py`, `tests/fake/test_admission_disk.py`, `tests/fake/test_admission_lease_waiters.py`, all five `tests/unit/test_policy*.py`, `tests/fake/test_policy_sandbox.py`, and `tests/unit/test_test_module_names.py`. One pytest process at a time under `/usr/bin/lockf -k /private/tmp/claude-501/subfleet-suites.lock`, with a job-specific `sf-ramp2-<pid>/` TMPDIR removed after each. Every lock hold was below 20 minutes. No runner remains.

Hypothesis: **1,340 passing generated examples**, 180 invalid cases, and **11 passing explicit examples** across 13 properties. The new rollback properties account for 120 unit + 30 daemon generated examples and one explicit example each. Counts:

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
| Policy whole-allowance | 150 | 0 | 8 |
| Policy initial-window R2 | 150 | 18 | 0 |
| Valid tier mixes | 250 | 38 | 0 |
| Invalid tier lists | 80 | 10 | 0 |

Mutation results (production source restored and checked by SHA-256):

| Mutation | Result |
| --- | --- |
| own-pass-count | Killed: 1 failed, 2 passed in 17.56s |
| count-attended | Killed: 1 failed in 2.37s |
| drop-reconstruction | Killed: 1 failed in 7.41s |
| process-memory-only | Killed: 2 failed in 4.24s |

The own-pass mutant is caught by immediate snapshot accounting (`used=0` after one commit); the new durable recheck independently prevents its former placement burst. Counting attended work incorrectly holds the dispatch; dropping reconstruction grants a start without the expected hold; process-only accounting reports zero usage after restart.

C-6.18 text changed:

A non-priority detached placement of any accepted non-turn job kind (`dispatch`, `resume`, `revive`, `gate-review`, or `handoff`), including a retry, commits only while the number of detached non-probe attempts whose `reserved_at` is later than `now - window_s` is below `K`. This includes future-dated reservations: a backwards wall-clock step can only make the ramp stricter, never grant a fresh burst, including after restart. The reserving transaction rechecks this budget against durable rows at the exact `reserved_at` it will insert, before taking leases; a spent budget holds that job and later ordinary jobs in the pass with the `ramp` reason and takes no new leases or disk reservation. Each enabled detached pass reconstructs from attempt rows and adds its own committed placements immediately; reservation checks reconstruct again because a clock rollback during workspace preparation can revive rows that expired at pass entry. Completed, failed and recovered reservations count too. Judged in commit order, every real elapsed window of `window_s` seconds contains at most `K` non-priority starts, including backwards clock corrections, provided positive wall-clock advances take at least that much elapsed time (forward clock jumps are outside a wall-clock ledger’s guarantee).

With free capacity, no continuing priority arrivals and a wall clock that eventually advances, every unchanged eligible finite queue eventually places in admission order. The following time bounds assume no backwards clock corrections; such corrections can delay expiry.

Evidence: [baseline](2026-10-11-placement-ramp-r1-baseline.json), [fixed suites and statistics](2026-10-11-placement-ramp-r1-suite.json), [four mutations](2026-10-11-placement-ramp-r1-mutations.json).

Unrun: the full repository suite, `test_admission_ramp_retention`, and separate `test_disk*` modules, outside this round’s permitted scope. Nothing requested within the permitted list remains unrun. Forward clock jumps that exceed real elapsed time are outside a UTC-only ledger’s guarantee; arbitrary backwards corrections are covered.

DONE
