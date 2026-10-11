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
