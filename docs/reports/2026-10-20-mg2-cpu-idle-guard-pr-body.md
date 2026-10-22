D-MG2 adds opt-in CPU idle and state-device service latency admission, with startup
priming and ten-second recovery. Live/default policy bytes are unchanged.

Round 1 fixes the capped concurrency measure: `disk_busy` now means **ms/op** on
the state-root device, with uncapped requests in flight alongside it. Both signals
use bias-corrected 60 s EWMAs, sampled at most once per second. Missing reads
preserve baselines and latches while holding nothing. Null/legacy-only policies
run no new native read. No reader starts a subprocess.

The daemon primes before its first detached pass. Release needs ten seconds of
readable clear data, rather than two 50 ms passes: +0.03 CPU idle, or disk service
10% below its threshold. `why` names readings and elapsed recovery. Status JSON
exposes values, units, ages, baseline and read cost. At most one minute sample is
recorded, retaining 1,440 samples plus their audit rows across restarts.

Merged cached `origin/release/217` **43f42d83d**, including the brief's **2eacd5806**,
without fetching. The contract change-log conflict retains both sides' entries
and every release clause. Daemon admission auto-merged; release liveness/priority,
routing and frozen-stream finalization changes remain. C-6.13/C-6.11 are amended.

**Proposal for Max under d1377, not applied:** background idle minimum **0.15**,
memory **warn**; session idle minimum **0.05**, memory **critical**. No load or disk
threshold. The old 0.80/0.90 disk fractions are withdrawn; the key now means ms/op.
The first ordinary 90 s AC recording produced 81 smoothed values at
1.662–2.264 ms/op (median 2.059), despite 6.95–76.98 requests in flight: the old
cap was 1.000 on every read. Its counter replay holds nothing under the proposal.
The deliberate saturation loop was **skipped** because `df -g` showed 67 available
before the experiment (52 at the final run), below the required >70 gate. No disk
threshold is suggested. The historical 10/11 shape would have been held by the
old disk term; its new service latency is **unknown**.

Final full read-point cost on AC, offered at 50 ms: **33.9 µs median / 1.33 ms p99
per tick**, **519 µs median / 12.45 ms p99** on ticks doing native reads. These
include policy checks and minute writes. Host scheduling delayed offered ticks;
the report compares the review's 330 µs / 3.0 ms and legacy 5.7 µs measurements.
Retention measured 0.918 MB with about 52 MB/day estimated WAL traffic plus
checkpoint I/O.

**Validation:** 392 required cases passed; the final strengthened feature gate is
247 passed. Nineteen selected regressions fail against the original guard files
without setup/collection errors. All eight named mutations were caught.
Whole local suites completed all 437 files: **15,241 passed, 11 failed, 98 skipped,
2 xfailed; zero unrun or partial files**. The failures are sandbox process/kernel
access cases, three unchanged importer assertions and two timing checks. Both
timing checks passed isolated retry; all original failures remain counted.
`test_prune_restore.py` passed all four cases. This is not a green whole suite.

Report and every file's counts: `docs/reports/2026-10-20-mg2-cpu-idle-guard.md`.
No push or remote CI query. Commits use workspace-local `.git-local` because the
shared Git metadata is read-only.
