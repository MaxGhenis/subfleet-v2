# Uncap admission and place by priority (plan, 2026-09-27, revision 4)

Max's ruling, 2026-09-27, in chat: "we should uncap everything and instead use
prioritization." On the desktop account, excluding it "makes sense if we're using
sf thru the cc app but not if thru the sf app". On 2026-09-28, relayed verbatim by
the release owner: "remove *all* caps", after "nothing should be queued wdym, we
lifted the caps".

This plan amends the acceptance contract (C-4.5, C-6.4, C-6.5, C-6.9, C-6.11,
C-10.3, C-11.2, C-11.3, C-18.1, C-26.9) and adds C-6.13. It builds on PR #59
(`feat/uncapped-turns`, final head 246230a8). PR #59 makes the two turn caps null
by default, stops turns waiting behind turns when uncapped, and keeps FIFO on a
lease for turns. It targets `release/217`, the line the installed daemon runs
(2.1.8, source b053e3de). Conversations, `tests/reference_scheduler.py` and
PR #59 exist only on that line.

Revision 2 answers the Astra plan gate's round 1 (changes requested, six
findings; see "Changes since revision 1" at the end). It also fixes two things
found while doing so: busy lanes never got a usage reading, and the
admission-probe reservation still refused the desktop lane however idle it was.

## What was measured

Measured at 2026-09-28T00:26Z (20:26 EDT) with `subfleet status`:

- 35 jobs pending, none placed for 2,175 s, and 0 lanes open. Holds were
  `behind-older-job` ×34 and `no-slot` ×1.
- Four Claude lanes (claude-1, -6, -7, -9) were open and each held 2 attempts,
  which is `max_in_flight_per_lane`. Five were closed on provider limits
  (claude-2, -3, -5, -8, -10), seven were disabled (claude-11 to -17), and one
  was excluded as `desktop` (claude-4).
- All six Codex lanes held 1 attempt each. Their readings were stale, so each
  was capped at `min(max_in_flight_per_lane, max_in_flight_unmeasured, 1)`. The
  literal 1 is in `scheduler.judge_lane`, so no policy value can raise it.
- 14 attempts were live against `max_active_attempts` 16. The fleet cap was not
  what bound; the per-lane caps were. The one `no-slot` job headed the
  `standard` tier, and C-6.9 held the 34 jobs that compete with it behind it.
- Load average was 93, 112 and 120 (1, 5 and 15 minutes) on 18 logical CPUs.
  Memory pressure was normal (`kern.memorystatus_vm_pressure_level` 1, 77% free).
- `~/.claude/sessions/*.json` held 32 live rows: 23 with entrypoint
  `claude-desktop` (7 `busy`, 16 `idle`) and 9 with `sdk-cli`. The `sdk-cli`
  rows were Subfleet lane runs, named by job id.
- At 02:45Z, after an interim bump to 6 per lane and 64 fleet-wide, idle lanes
  had fresh readings and every lane with an attempt in flight had stale ones.
  For example, codex-3, -5 and -6 (idle) were fresh, while codex-1, -2 and -4
  (one attempt each) were stale. The timer's usage probe waits for an idle lane
  (C-18.1).

## What counts as a concurrency cap

Removed (default `null`, meaning no cap; a positive whole number still sets one):

| Key | Was | Where it bound |
|---|---|---|
| `caps.max_in_flight_per_lane` | 2 | `judge_lane` `no-slot` |
| `caps.max_in_flight_unmeasured` | 1 | the same, while the lane has no fresh reading |
| the literal `1` in `judge_lane`, `capacity.open_lanes`, `policy.lane_capacity`, `timers` | 1 | the same; no policy value could raise it |
| `caps.max_active_attempts` | 4 by default, 16 live | the fleet block; C-6.9's kept slot; "at the cap the pass ends" |
| `caps.max_active_attempts_per_parent` | 1, hidden in `_parent_blocks` and absent from `DEFAULT_CAPS` | `parent:` capacity block |
| `conversations.max_active_turns`, `turn_slots_per_lane` | 3 and 1 | PR #59 makes both null by default |

Kept, because they are not concurrency: `max_attempts`, `max_wall_s`,
`gate_max_rounds`, `max_tokens_observed`, the workspace timeouts and retry count
(C-6.8), and `keepalive_workers` and `probe_timeout_s` (timer pools, not
admission).

Also removed (revision 3, after "remove *all* caps"): the two submit counts,
`max_child_jobs` (8 children one parent may create in all) and
`max_writable_per_session` (8 unfinished writable jobs one session may hold,
C-6.5). Revision 2 kept them as runaway backstops, which answered round 1's
finding 1 one way. Max's instruction answers it the other way: both are null by
default, and a policy may still set either. What C-6.5 refuses and is not a count
stays: a second writer in one checkout, a second live instance of one session,
and a second job on one output path.

The probe lease stays. While an admission or timer probe holds a lane, no job
starts there (`slot_block`). That is exclusivity for one measurement, not a count.

## Amendments

### A1. C-6.4, caps

`max_in_flight_per_lane`, `max_in_flight_unmeasured`, `max_active_attempts` and
`max_active_attempts_per_parent` are each null by default (no cap), or a positive
whole number when the policy sets one. The shipped `subfleet/default_policy.json`
says null for all four and carries the `admission` block. The loader accepts null
for these four and for the two turn caps only. It refuses 0, negatives, booleans,
strings and fractions, naming the key. `max_active_attempts_per_parent` joins
`DEFAULT_CAPS`, so it is visible. A lane with no fresh `provider` reading is capped
by `max_in_flight_unmeasured` when that is set, and otherwise by
`max_in_flight_per_lane` when that is set. The literal 1 goes. `policy.cap(caps,
key) -> int | None` and `policy.lane_slot_cap` are the only readers of these keys,
as `turn_cap` is for turns.

### A2. C-11.3, how a lane is chosen once nothing caps it

A per-lane cap used to spread load. Without one, every Claude job would go to the
lane with the most headroom, and every Codex job to the lane whose weekly window
resets first, until a provider limit stopped it. The limit would then hit every
job on that lane at once.

The comparators gain a leading load band:
`band = in_flight // admission.lane_spread` (default 2; null means no bands).
Candidates are ordered by band first, then by the C-11.3 comparator, unchanged,
within a band. With `lane_spread` 2, lanes fill to 2 attempts each in today's
order, then to 4 each in the same order, and so on. With a per-lane cap equal to
`lane_spread`, the choice is exactly today's. C-11.3's sentence "In-flight counts
never reorder Codex lanes" becomes "in-flight counts reorder Codex lanes only
across bands". `in_flight` stays the job's own pool (C-26.9).

A `desktop` lane that is a candidate (A5) sorts after every other candidate for
that model: `rank_key` starts with the lane's `desktop` flag, carried in the
lane's detail. A turn's affinity (C-26.2) comes after that flag and before the
band.

### A3. C-6.9 and C-26.9, the order jobs are placed in

Each pass considers its jobs in one order:

1. **Class.** `attended` is a conversation turn from the Subfleet app. Turns keep
   their own pass, which runs first (C-26.9). `session` is a gate round, or a
   detached job someone is waiting on now. That means its `caller_session` is
   named by a **validated** `~/.claude/sessions` row (the row's pid is still the
   process that wrote it, A5), or its parent job is unfinished. `resume` and
   `revive` jobs are classed the same way. `background` is everything else: a
   caller that has exited, a launchd or cron submission, a batch whose dispatcher
   is gone. A batch submitted by a live session is `session`, because that session
   is waiting on it. A caller's `caller_pid` alone is **not** evidence: a live
   pid proves a process, not the caller (review finding 6).
2. **Tier**, in the policy's declared order, as today.
3. **FIFO**: `created_at`, then store row order.

Liveness is read once per pass, off the store lock. `scheduler.priority_class(job,
live)` and `scheduler.ordered_jobs(policy, jobs, live)` are pure functions of it.

C-6.9's hold-back (`behind-older-job`, `slot-kept`) applies only while the job's
pool has a count cap (`scheduler.pool_capped`): for a detached job, a fleet or
per-lane cap; for a turn, either turn cap; for a job with a parent, also a parent
cap. With every count cap null (the new default), no job waits behind another.
With no slots, a later job never takes a slot an older one needs. Tiers still
never hold each other. When a pool is capped, the old rule and its `slot-kept`
slot apply in the new order: a waiting `session` job holds back later competing
`background` jobs of its tier.

Two scarce things are not slots, and each keeps its FIFO with no cap at all:

- **A lease** (a writable checkout, an output path, a conversation, a native
  session). PR #59 keeps FIFO on a lease for turns. A turn waiting for a lease
  queues for it, and a later turn that needs it is held `lease-held` with
  `queued` and `queued_behind`. The queue no longer asks the job's kind, so it
  covers detached jobs too. Submit already refuses a second writable job in one
  checkout and a second job on one output path (C-6.5), so detached contention is
  rare. The queue costs nothing, though, and keeps FIFO for what remains now
  that C-6.9's hold-back is gone.
- **A lane's probe lease** (`lane:<id>:slot:0`, C-11.4). With no per-lane cap, a
  busy lane almost always had an attempt on `slot:0`. An older writable job
  waiting to probe that lane then lost the slot to every later job that needed no
  probe (review finding 2). Detached attempts now number their slots from 1, as
  turns already number theirs apart (`slot:turn-<n>`, C-26.9), so `slot:0` belongs
  to probes alone. A probe waits only for another probe, never for an attempt.

### A4. Provider limits are the bound: C-4.5, C-9.6 and C-18.1

Without count caps, the fleet is bounded by provider limits and by
`headroom_floor`, which refuses a lane at 85% of any window. What happens on a
limit is unchanged. The closure is recorded at finalization (C-9.6), the lane is
no candidate for that scope until the reset, and the job's next attempt goes to
the next candidate in the same priority order (C-4.5). The job also keeps its
existing exclusion of every lane it was limited on.

The floor only works on a fresh reading. The periodic usage read (C-9.9's
`oauth/usage` for Claude, the usage endpoint for Codex) waited for an idle lane
(C-18.1), and without caps a lane is seldom idle. **C-18.1 changes: a busy lane is
read too.** The read costs no model turn and takes no slot. Its holder
(`probe:timer:usage:<uuid>`) holds no lease, and it spends no heal turn
(C-23.47). A heal waits for an idle lane, and on a busy one the running attempts
renew the token themselves; so a busy lane's `expired-token` is not published at
all (publishing it would latch a working lane until it drained). If the read finds
the credential dead (`auth-dead`, `revoked`, `auth-revoked`, `no-auth`), it takes
a fence of its own, `lane:<id>:slot:fence`, until the cycle publishes the verdict
(C-23.44). The fence is never contended: an admission probe may hold `slot:0`.
Admission reads any `probe:` lease on a lane as a slot block, so it cannot place
work there in between, and no job waits for the fence. The cycle releases every
holder it took, whatever raised. A keepalive, which spends a turn, still waits for
an idle lane.

`max_attempts` counts every attempt, as before, `limited` ones included.
Revision 3 stopped counting limits. Review showed that a job limited on every
lane that serves its model then had no candidate and waited out `max_wall_s` (6
hours), ending `cancelled` with rc 130, where it had failed at once with rc 4
(C-17.3). Uncapping is not a reason to change retry accounting, and the brief said
to keep `max_attempts`.

### A5. C-10.3, the desktop login

A lane whose identity is the desktop app's (C-10.3, unchanged) is **excluded
only while Claude Code is using that login**. Otherwise it is a candidate that
sorts last (A2).

The signal is Claude Code's own registry, `~/.claude/sessions/<pid>.json`,
together with one process-table read (`procs.snapshot`, the `ps` the daemon already
uses; one read every 2 s at most):

- **A row is alive** only while its pid is live and, where the row records
  `procStart`, that process started then (`registry.validated`). A stale row
  whose pid another process reuses is dead (review finding 6). A row without
  `procStart`, from an older Claude Code, is alive on its pid.
- **A live row counts** unless it is a Subfleet run: an `sdk-*` row whose process
  belongs to a live attempt (its process group is the attempt's `pgid`, or it is
  the attempt's child) or whose session a live attempt runs
  (`native_session_id`). The entrypoint alone proves nothing about the
  credential. Any other headless run may use the desktop login, so it counts
  like the Claude app's rows (`claude-desktop`) and a terminal's (`cli`) (review
  finding 4).
- **A counted row is use** while its `status` is `busy`, or its status time
  (`statusUpdatedAt`, else `updatedAt`) is within `admission.desktop_recent_s`
  (default 1800) of now. A row with no status, or a status and no time to judge it
  by, is unknown and counts, whatever its start (review finding 5).
- **What cannot be read is use.** That covers a registry directory that cannot be
  listed, and a row file whose process is live but whose content cannot be parsed
  (review finding 5). An absent directory means nothing is running.

This matches Max's rule. Working through the Claude Code app keeps a session
busy or recently active, so the login is protected. Working through the Subfleet
app runs turns as attempts, so after 30 minutes with no Claude Code activity the
desktop lane opens, last in line.

The answer is read at most every 5 s, off the store lock. It is put on the desktop
lane row as `desktop_in_use`, which joins `scheduler.LANE_FACTS`; a desktop lane
without it counts as in use. **The reservation reads it again** (review finding
3). Before each reservation try, the daemon refreshes the answer off the lock (at
most 5 s old). Inside the transaction, `_route_rows` marks the desktop lane with
that current answer, not the early view's. A change since the evaluation changes
the lane's facts, and C-6.3's check judges that lane again. The admission-probe
reservation, which refused a desktop lane outright, now refuses it only while in
use.

A change, and the first answer after a start, is recorded as a `desktop.in_use`
event (in_use, was, and the counts of `busy`, `recent`, `unknown` and `subfleet`
rows), so a placement on the desktop lane can be audited. Only the detached
admission pass writes it. Read ops (`status`, `why`, `lanes`) read the signal and
never write. `allow_desktop` still admits the lane while it is in use, and it
still sorts last. `capacity.open_lanes` and `status.json` count the desktop lane
as open when it is not in use. Timers (usage probes, keepalive, auto-touch) still
never use the desktop lane.

### A6. C-6.11, holds

`fleet-full`, `slot-kept`, `parent-cap` and `behind-older-job` can occur only in a
capped pool. In an uncapped pool, `no-slot` means only a `slot_block` (a probe or
a latched credential). `lease-held` can name `queued` leases, kept for an older
job, as well as held ones. A new hold, `machine-busy`, comes from A7. It is
ordinary queueing, in `EXPECTED_HOLDS`, and never logged as a warning.

### A7. New C-6.13, a machine guard (proposal, overridable)

Load came from what jobs run, not from Claude. The brief measured it at about
20:15 EDT on 2026-09-27: Claude processes summed to about 31% CPU, while a `bfs`
search took 453% and test suites and `uv` builds about 430%. Uncapping can
admit twice as many such jobs on a machine already at load 110 to 120. The
daemon starved at that load before the 2.1.8 QoS clamp.

**Proposal: hold low-priority detached jobs at the door while the machine is
saturated, and never hold a turn.** The guard is `admission.machine_guard`:

```json
"machine_guard": {
  "background": {"load_per_cpu": 6.0, "memory_pressure": "warn"},
  "session":    {"load_per_cpu": 10.0, "memory_pressure": "critical"}
}
```

A job of a class is held `machine-busy`, with the reading and the threshold.
The hold applies while `max(load1, load5) / logical CPUs` is at or above the
class's threshold, or while `kern.memorystatus_vm_pressure_level` is at or
above its level (warn 2, critical 4). Using the larger of the 1- and 5-minute
averages holds quickly and releases slowly, so a momentary dip does not release a
burst. On 18 CPUs the thresholds are 108 for background jobs and 180 for session
jobs. At 20:26 EDT background jobs would have been held, and session jobs placed.

The check is O(1) and made before workspace preparation, so a held job costs no
git. It is not a count: it never compares how many jobs run. `null` disables it,
and either class can be removed. An unreadable signal holds nothing.

This is Max's call. Revision 2 shipped it on. After "remove *all* caps" and
"nothing should be queued", revision 3 ships it **off** (`machine_guard: null`).
The mechanism and its tests stay, with the thresholds above as
`policy.MACHINE_GUARD_PROPOSAL`. Whether to turn it on is queued as a decision
(`cos add-decision`).

### A8. C-26.9, turns

PR #59's text stands: turn caps null by default, no turn behind a turn when
uncapped, FIFO on a lease. Added: turns are the `attended` class, the machine
guard never holds them, and they take the desktop lane only as A5 allows.

## Invariants and the tests that check them

Property tests use Hypothesis. `tests/admission_model.py` is a pure model of one
pass: jobs in priority order, each evaluated against the view as updated by the
placements before it, then held or placed. Daemon-level tests run the real pass
with fake providers.

1. **Nothing waits for a count that isn't there.** With every count cap null and
   the guard open, the model places every job for which `evaluate` chooses a
   lane. It holds none for `fleet-full`, `slot-kept`, `parent-cap` or
   `behind-older-job`, and `evaluate` gives `no-slot` only with a `slot_block`.
2. **Priority order.** The model and the daemon consider jobs in (class, tier
   rank, `created_at`, row order). The daemon's order is checked by reservation
   row order. In a capped pool, a job is never placed on a slot that a held,
   earlier, competing job of its tier needed.
3. **Scarce things keep their FIFO.** An older writable job's probe is reserved
   while later read-only jobs run on the same lane, and no attempt ever holds
   `slot:0`. A lease freed mid-pass goes to the job that queued for it first
   (PR #59's tests; the queue no longer asks the job's kind).
4. **Determinism.** For a fixed policy, view, jobs, liveness and machine reading,
   `evaluate`, `ordered_jobs` and the model give the same answer on every call,
   whatever order the rows arrive in.
5. **Monotone in caps.** Treating null as infinity, raising any cap never removes
   a lane from any job's candidates. The all-null model places at least as many
   jobs as the model under any caps. Pairwise monotonicity of the greedy count is
   tested too, and Hypothesis found no counterexample.
6. **Desktop.** While the login is in use, no job without `allow_desktop` is
   placed on a desktop lane. While it is not, a desktop lane is chosen only when
   no other lane is a candidate for that model. A view without the signal behaves
   as in use. The signal is monotone and fails closed: adding a row, widening the
   window, owning fewer processes, or reading fewer row files never frees the
   login. A reused pid is not a session. The reservation reserves nothing on the
   desktop lane once Claude Code becomes active after the evaluation.
7. **Spread.** Uncapped lanes never differ by more than one band. With
   `lane_spread` equal to a per-lane cap, the choice equals the capped choice.
8. **Guard.** Turns are never held. Raising the load never places more jobs. A
   class held implies every lower class held (proposed thresholds). With the guard
   null, nothing is held for the machine.
9. **Differential.** `scheduler.evaluate` equals `tests/reference_scheduler.py` on
   drawn cases, now including null caps, `lane_spread`, `desktop_in_use` and
   parent caps. The reference implements each of those in its own words.
   `route_check.still_stands` equals an evaluation now (the C-6.3 suite), with
   the desktop signal flipping between the two.
10. **Limits and readings.** A job limited on every lane still fails at once with
    rc 4. A busy lane gets a usage
    reading with no lease. A dead verdict on a busy lane fences it until
    published, and a keepalive still waits for an idle lane.

## Rollout

1. The branch is `feat/uncap-admission-priority`, on PR #59's final head
   (246230a8, which already contains PR #60's CI fix), and the PR targets
   `release/217`.
2. Independent review: `subfleet gate pr --peer astra`, or an Opus review lane.
   Merge when `gh pr checks` passes and the PR is MERGEABLE.
3. Release: the "Subfleet desktop transition" session cuts and installs desktop
   releases (2.1.9). I hand it the merged sha and the policy edit rather than
   install in parallel. The live `~/.subfleet/policy.json` pins caps (64 and 6
   since the interim bump at 01:49Z, and the unmeasured 1). It must set all four
   to null, or drop them, and gain the `admission` block, **at install time**:
   2.1.8's loader refuses null caps (checked).
4. Measure after, as before: `subfleet status`, `daemon.status` `admission`
   (pending, `idle_for_s`, reasons, `open_lanes`), load average, the classes
   placed and held, and reading freshness on busy lanes, at about 5 and 30 minutes
   after the restart.
5. `main` has the same caps but no conversations or reference scheduler. The
   port there is a follow-up task.

## What this does not do

- It does not change which models a task may use, the reserve (C-11.7), when a
  probe is required (C-11.4), closures, or the floor.
- It does not decide who the desktop login is. C-10.3's identity rules stand; A5
  only decides when that lane is excluded.
- It adds no knob for per-job priority. A terminal `subfleet run` outside a Claude
  Code session is `background` unless its parent is live.

## Changes since revision 3 (Astra round 2 on revision 3; Opus review of PR #72)

- Every attempt counts toward `max_attempts` again (A4). Opus finding 1 showed the
  job limited on every lane that then waits six hours; CI's
  `test_credits_rejection_closes_model_and_retry_explains_exclusion` failed on
  it.
- A busy lane's dead verdict is fenced by `lane:<id>:slot:fence`, never contended.
  This answers Astra finding 1 and Opus finding 3.
- A busy lane's `expired-token` is not published (Opus finding 4).
- The probe cycle releases every holder it took, whatever raised (Opus finding 2).
- A registry row whose `pid` is missing, not a number, or not its file's is
  unknown, and counts as use (Astra finding 2).
- `status.json` judges the desktop lane with the in-use signal (Astra finding 3):
  the daemon hands `Timers` its `_desktop_in_use`.
- The admission-probe reservation reads the in-use answer fresh, as the attempt
  reservation does (Opus finding 6).
- With only the parent cap set, a job waits only behind an older job that shares
  an ancestor with it (`scheduler.hold_scope`, Opus finding 5). The parent cap is
  counted per family, so an unrelated waiter cannot take anything the job needs.
- An empty registry costs no process-table read.

## Changes since revision 2 (Max, 2026-09-28: "remove *all* caps")

- `max_writable_per_session` and `max_child_jobs` are null by default, like every
  other count cap. The non-count refusals of C-6.5 stay.
- The machine guard ships off. Its mechanism, tests and proposed thresholds stay,
  and turning it on is Max's decision.
- Rebased onto `release/217` at 72a5bd9a, which contains PR #59 and the 2.1.9
  conversation fixes. The live daemon runs 617892c1 (#59 installed at 12:41Z).

## Changes since revision 1 (Astra round 1: changes requested)

1. `max_writable_per_session` is a count: it is now named as an explicit
   exception, kept by the brief's instruction, and part of Max's decision.
2. Probe starvation: detached attempts never take `slot:0`, so a probe waits for
   no attempt (A3); daemon test `test_c11_4_detached_work_never_keeps_an_older_writable_job_from_its_probe`.
   It fails with the old numbering.
3. Stale desktop answer at reservation: refreshed off the lock before each try
   and read in `_route_rows` (A5); daemon test
   `test_c10_3_c6_3_the_reservation_sees_claude_code_become_active`. It fails
   when the early answer is reused.
4. `sdk-*` is not proof of the credential: exempt only when a live attempt owns
   the process or session (A5).
5. Missing activity fields and unreadable live rows count as use (A5).
6. Pid reuse: rows are validated against their recorded `procStart`, and a bare
   `caller_pid` no longer makes a job `session` (A3, A5).

Also: the lease queue no longer asks the job's kind (A3), busy lanes are read (A4,
C-18.1), and the admission-probe reservation honors the in-use signal (A5).
