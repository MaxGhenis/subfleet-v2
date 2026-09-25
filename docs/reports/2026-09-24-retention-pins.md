# Retention pins and the unreachable byte budget, 2026-09-24

A proposal, not an implementation. Nothing here changes code; C-8.4 is unchanged
until Max rules on the bound in "Decisions" below.

## What was observed

The formal-verification review of 2026-09-24
(`~/reviews/formal-verification-2026-09-24/verify-s7-subfleet.md`) reported that
retention pruned the morning's survey jobs about 3.5 hours after they finished
while it kept older jobs, and that `daemon.log` had repeated
`worker retention failed: TimeoutError (16 in a row, next try in 60 s)`. It
attributed the first to C-8.4's unread-notice pin growing without bound. It also
noted that `_maintenance` runs `shutil.rmtree` between its two pin re-checks
(`subfleet/retention.py`, the `retention.selected` and `retention.pruned`
transactions).

The measurements below come from a consistent copy of the live store, taken
with the SQLite backup API from a read-only connection at about 17:27Z on
2026-09-24. The worktree sizes come from a read-only walk of
`~/.subfleet/worktrees`. The scripts live in this session's scratch directory
and are not part of the repository.

| Measure | Value |
| --- | --- |
| Jobs / terminal / pinned by `_pins` | 359 / 342 / 342 (325 terminal) |
| Terminal jobs not pinned | 17, all submitted on 2026-09-24 |
| Bytes, all jobs (job directories plus owned worktrees) | 9.43 GiB |
| Bytes held by pinned jobs | 9.43 GiB; the budget is 2 GiB |
| Job directories | 410 MB; walked in 0.24 s |
| Daemon-owned worktrees | 32 (31 present), 9.71 GB; walked in 18 s on an idle machine |
| One `_pins()` call | 25 ms median, most of it the actions × jobs `_contains` loop |

Pins by the first rule that holds each job:

| Rule | Jobs | GiB | With owned worktree |
| --- | --- | --- | --- |
| Non-terminal | 17 | 0.01 | 0 |
| `gate-review` kind | 75 | 0.05 | 0 |
| Quarantined or live attempt | 7 | 0.90 | 4 |
| Lease | 1 | 0.00 | 0 |
| Parent of another job | 12 | 0.00 | 0 |
| Salvage artifact | 67 | 8.44 | 27 |
| Unread notice with a session | 36 | 0.02 | 0 |
| Unread notice with no session | 127 | 0.00 | 1 |

## What it means

**The budget is unreachable, and salvage holds the bytes.** `_maintenance` walks
jobs oldest first. It stops only when `count <= max_jobs and total <= max_bytes`,
and `total` includes pinned jobs. Pinned jobs alone hold 9.43 GiB, 8.44 GiB of it
in the worktrees of salvage-pinned writable jobs. So the byte condition never
holds, and every pass prunes every unpinned terminal job whatever its age.
Those jobs are the newest ones, a few kilobytes each. Their bytes cannot bring
the total under 2 GiB. The review was right that retention deletes the newest
evidence. The mechanism is the byte budget against salvage pins, not the
notice pin.

**The daemon never tells retention that a salvage ref is held elsewhere.**
`Daemon._retention` calls `maintenance(...)` without `salvage_referenced_elsewhere`,
so every salvage artifact pins its job forever. The ref itself lives in the
caller's repository (`refs/subfleet-salvage/...`). The job row is only the record
of it. `_remove_worktree` already refuses to remove a dirty worktree unless a
recorded salvage ref's tree matches the files exactly, so the worktree is
recoverable from the ref once that check passes.

**The unread-notice pin is a count problem, not a byte problem.** 221 of the 261
unread notices have `session_id` NULL because the job's caller had no Claude
session: 75 are `gate-review` jobs (which the kind rule pins anyway), 135
dispatches and 11 resumes. `notice.pending` filters `WHERE session_id=?`,
so these notices can never be read, and they pin their jobs forever. Session
notices date back to 2026-09-19. While the count limit (500) is not binding, this
costs nothing. Once it binds, it will prune the newest jobs the same way.

**The deadline.** Every pass sizes every job, including 9.7 GB of pinned
worktrees that no pass can remove. That walk took 18 s on an idle machine.
`_pins` is cheap at this size (25 ms, twice per candidate). An interrupted pass
returns everything as protected, and the next pass starts over, walk included.
Whether the walk alone exceeded the 60 s deadline under the load of 2026-09-24
is unverified: `daemon.log` lines carry no timestamps.

**The rmtree window is real but rare.** Only three things can pin a terminal job
between the two re-checks. A `resume` names the job as its parent. A gate action
cites it, but gate-review jobs are pinned by kind anyway. A lease is taken on its
worktree, but in-place admission is fenced by the `retention:<job>` lease.
Notices only move toward acknowledged. In the resume case, the new job can be
admitted into a worktree that `_remove_worktree` removes a moment later. The
final re-check then keeps the row, because it is now a parent, but not the
files. No incident is known.

## Proposal

1. **Stop charging preserved worktrees to the budget.** After a writable job is
   terminal, remove its daemon-owned worktree once `_remove_worktree`'s own
   check proves a recorded salvage ref preserves it. The job row stays, and so
   does its salvage artifact, the record of the ref. This is a separate step
   from pruning the job. It frees about 8.4 GiB here without losing any record.
   C-8.4's "hold salvage refs referenced nowhere else" then pins rows, which are
   small, not worktrees.
2. **Pin only what can still be read.** An unread notice pins its job only when
   it has a recipient session and is younger than a bound (proposed: 7 days,
   matching the CLI's `INBOX_KEEP_S`). The sessions registry could instead tie
   it to session liveness, but that makes pruning depend on a `ps` call. A
   notice with no session never pins. Keep writing it, since `runs show`
   displays it.
3. **Never prune for an unreachable budget.** Compute the bytes of pinned jobs
   first. If they alone exceed `max_bytes`, prune for the count limit only, and
   write one warning a day to `daemon.log`, e.g. `retention: pinned jobs hold
   9.4 GiB, over the 2 GiB budget; nothing unpinned can fix that`. An operator
   then sees the cause instead of losing the newest evidence.
4. **Make a pass cheap and resumable.** Record a terminal job's bytes once, in
   its `retention` bookkeeping or an event keyed by job id and directory mtime,
   and walk only jobs that are not terminal or whose mtime changed. Keep the
   computed pin set and sizes across an interrupted pass rather than starting
   over. Compute `_pins` once per pass, plus a targeted per-candidate re-check
   of the few rows that can change: parent, lease, gate action.
5. **Close the rmtree window by deleting rows first.** In the `retention.selected`
   transaction, re-check the candidate's pins and delete its rows. Record the
   directory, worktree and salvage refs to remove in the event, which retention
   keeps. Remove files after commit. A sweep removes `jobs/<id>` directories that
   no row names and a pruning event does, and counts their bytes until then. A
   resume that races now gets "no such job" at submit instead of a job pointed
   at a removed worktree. The alternative, a `prune:<job>` fence that submit and
   gate code must check, fails open whenever a new pin source forgets it.

## Test plan

- Unit, `tests/unit/test_retention*.py`:
  - Pinned bytes over budget prunes nothing for bytes and logs once.
  - Null-session and expired notices do not pin; fresh session notices do.
  - A salvage-preserved worktree is removed while its row and salvage artifact stay.
  - A dirty worktree without a matching ref is kept.
  - Rows-first ordering: a parent pin taken after `retention.selected` finds no row.
  - The orphan sweep removes an interrupted deletion.
  - An interrupted pass resumes without re-walking.
- Fake daemon: a writable job finishes with a salvage ref and a notice for a
  session that never reads it. Then the worktree is gone, the job and ref record
  remain, and a newer read-only job survives the next pass.
- Invariant for the PR, property-tested: no pass prunes a job while a younger
  unpinned job survives; pinned jobs are never pruned; and bytes pruned for the
  byte limit are zero whenever pinned bytes exceed it.

## Proposed C-8.4 text

> **C-8.4** Retention: newest 500 jobs or 2 GiB, whichever binds, computed outside transactions in a maintenance pass, over jobs that retention may prune; when the jobs it may not prune already exceed 2 GiB, it prunes for the count alone and says so in `daemon.log` once a day. Jobs that are active, `quarantined`, have a notice for a caller session that is unread and younger than 7 days, hold salvage refs referenced nowhere else, or are a gate's evidence are never pruned; a notice with no caller session pins nothing. A terminal writable job's worktree is removed, and its bytes stop counting, once a recorded salvage ref is proved to preserve it; its row and the ref's record stay. A pruned job's rows are deleted, after one last pin check, before its files; a directory no row names is removed by a later pass. Probe and keepalive results live in `readings` and `events`, not in `jobs`.

## Decisions

- The notice bound: 7 days, session liveness, or another rule.
- Whether gate-review jobs stay pinned for good, or only while their gate is
  open and for a bound after it closes. They hold 0.05 GiB today, so this is
  about the count, not the bytes.
- Whether to build this now. Items 1 and 3 alone stop the loss of new evidence.
