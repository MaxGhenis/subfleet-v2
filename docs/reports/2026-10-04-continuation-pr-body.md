# Durable unattended conversation continuation

A turn can dispatch background runs, end, and receive one durable Subfleet message when its results are ready. This round restores receipt-only `subfleet wait` / `run --wait`: a waiter returning after the turn ends cannot acknowledge and swallow the only wake. Already-delivered runs resolve only their own trigger kind; timer and PR alternatives remain armed. Missing registered runs finish as `pruned`.

PR re-arms retain the latest fired request's snapshot and use turn-job creation as the final-text event threshold, including CI results arriving during the turn. Undelivered observations cannot suppress a new wake. Refused PR targets persist per conversation and re-arms are refused without another refusal turn. Timers require five minutes from both now and turn creation.

Idle evaluation reads eligibility, batches targets and runs once per second. For 40 conversations with 16 running runs plus a timer, paired CPU measurements fell from 17.81 to 3.26 ms per forced evaluation, or 0.14 ms per production control tick. Idle writes fell from 40 to zero; timer-only long-poll reader checks fell from 6,326–9,089/s to 2/s.

A blocked Claude conversation clears only when the catalog transcript mtime is strictly after blocked_at, the fresh outside-writer check finds no writer, and no Subfleet turn or conversation/native lease remains. These checks are paced at five seconds.

Validation: 115 wake/review unit cases (31 reviewer repros included), 35 catalog cases, 100 notice cases, 4 ledger cases, 62 MCP cases, 212 conversation-service/store cases and 98 fake cases passed: **626 distinct passes**. Five wake Hypothesis properties and seven mutation checks pass; the 13 mutation-target cases pass again after restoration.

Validation remains partial: one unit case failed on unavailable macOS boot identity; one catalog cleanup case was excluded after denied `/bin/ps`; 20 process-inspection cases skipped, including both strand e2e cases, all four real wake e2e cases and nine milestone-1 cases. One optional admission-liveness property was interrupted at 8m56s. The review's pre-existing flaky conversation e2e and saved steer counterexample were not changed. Skips and interrupted work are not counted as passes.

`app/build.sh` passed in **278.45 seconds**, validating the plist and signing the bundle. The app was not installed or launched.

[Round-two report](2026-10-05-continuation-r2.md) records findings, per-suite counts, paired measurements, mutations and validation limits. C-15.3, C-24.10 and desktop design D-28 are updated. The hub can post this prepared body; this worktree made no PR edit or push.
