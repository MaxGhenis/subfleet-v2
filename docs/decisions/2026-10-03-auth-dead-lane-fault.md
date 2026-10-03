# An auth-dead lane is the lane's fault; prove a lane before a burst (2026-10-03)

Max delegated Subfleet design calls. This record makes two of them, for 2.1.9
on `release/217`. It amends the acceptance contract at C-4.5, C-9.3 and C-23.44,
with C-6.11, C-11.3, C-11.4 and C-15.1, and it adds C-6.14.

## What happened

On 2026-09-30 at 15:43Z, admission had placed nothing for 6,742 s
(`daemon.log`: "admission: placing again after 6742 s idle"). claude-5
(mghenis@gmail.com) then came above its floor. Every other Claude lane was
limited, held or below its floor, and per-lane caps were off. Within 40 s
admission put 37 jobs from about 20 sessions on claude-5.

The organisation had turned off Claude Code subscription access for that
account. Each attempt's stream (for example
`~/.subfleet/jobs/20260930-114330-salvage-r3-cont3/a1/stdout`, read only) had
SessionStart hook events and a `system/init`. Then came Claude Code's
placeholder frame: model `<synthetic>`, `is_api_error_message`, error
`oauth_org_not_allowed`, every usage counter zero, and the text "Your
organization has disabled Claude subscription access for Claude Code". It ended
with a `success` result marked `is_error`, `api_error_status` 403 and
`duration_api_ms` 0. The CLI took 78 to 181 s of wall time to get there under
the load. The adapter classed each attempt `auth-dead`. Under the installed
2.1.9 (`daemon.py` near line 4785 in release 20260928T124024Z), C-4.5 retried
only `limited` (unpinned), `transient`, and `lost` on a read-only job.
`auth-dead` required reconciliation, so all 37 jobs ended `failed` with rc 5
after one attempt each. C-23.44 disabled claude-5, but only after all 37 had
launched. Example jobs: `20260930-114330-salvage-r3-cont3`,
`20260930-113744-viewer22-review`, `20260930-114331-titles-cont3`.

## Call 1: an auth-dead lane is the lane's fault (C-4.5)

**The rule.** An `auth-dead` attempt is a *lane fault* when all of these hold:

- the job has no lane pin;
- the job is no conversation turn, since its conversation decides a turn's
  failover (C-26.7);
- the attempt left the workspace as it found it. Either the job is read-only,
  or its salvage at finalization found the end tree equal to the attempt's start
  snapshot (no ref written, nothing left out, no error) with HEAD where the
  attempt began.

On a lane fault, the job moves on to the next candidate as it would after
`limited`. The lane is disabled as before (C-23.44). The attempt records
`lane_fault` in its evidence, `daemon.log` says the job moved on, and the job's
eventual notice names each lane it moved on from. A pinned job, or one whose
workspace changed, keeps today's behaviour: it ends `failed` with rc 5 for
reconciliation.

**Not counted, rather than one more attempt.** `max_attempts` does not count a
lane fault. A job may have `max_attempts` attempts besides its lane faults.
The reasons:

- A lane fault is the fleet finding a dead lane, not the job failing. Charging
  it to the job would spend the job's budget on the fleet's state.
- Lanes can go dead together: several accounts, or a provider-side refusal for
  a while. "One more attempt" would fail a job on the second dead lane even
  while a third lane worked.
- The count is bounded anyway. Each lane fault disables its lane in the same
  transaction that decides the retry, and a disabled lane is never a candidate
  again until re-enrolment gives it a new id (C-1.3). So a job moves on from at
  most as many lane faults as there were lanes to try. When none is left, the
  job waits, held for good (C-11.8). It does not fail. Cancelling it is a
  person's call.

**Why the tree test, and nothing about the model.** The request asked for the
tree test, and it is the right safety line. A writable job that changed nothing
in its worktree, at the same HEAD, has nothing to reconcile. A read-only job
was already treated as safe to run again (`lost` retries).

I first also required "the model never answered". I dropped that because it
would fail a read-only job whose lane was revoked mid-run. The hazard it was
guarding against is better fixed at the source (below). Each lane fault's
evidence still records `model_answered`.

**A prerequisite: `auth-dead` only from the CLI's own words (C-9.3).** The
Claude classifier matched its organisation-block and credential phrases
against every assistant frame, the model's included. A review of this very
classifier quotes "Your organization has disabled Claude subscription access".
Today that disables one lane. With moves, the job would disable every lane it
reached. Now only these count as evidence: stderr, the frames no model
answered (Claude Code's placeholders), the result's `errors`, and its text when
it is marked `is_error`. A test pins that a model quoting the phrases ends
`ok`. Codex already read only its failure events and stderr.

## Call 2: prove a lane before a burst (C-6.14)

**The rule.** A lane is proven while a model has answered on it within
`admission.prove_idle_s` (900 s; null turns the hold off). On a lane that is
not proven, a detached attempt in flight that has not answered is the lane's
pilot. While a pilot is in flight, the lane is `no-slot` to every other
detached job, with `slot_block` `proving:<attempt id>`. Those jobs go to other
lanes or wait. The pilot's first answer proves the lane and frees it at once. A
pilot that ends without an answer hands the hold to the next job. A pilot that
goes `auth-dead` disables the lane while the rest of the burst is still queued.
Turns are never held and are never pilots.

**What counts as an answer.** The first non-synthetic assistant event, or usage:

- Claude: an `assistant` frame a model served, any `usage` counting a token, or
  `system/thinking_tokens` with a positive count. A real 2026-10-03 stream
  (`20261003-101520-mpv-mg-openmessage`) shows Claude Code writing these while
  the model thinks, before the first assistant frame, so a long-thinking pilot
  proves its lane early.
- Codex: a thread item only the model produces, or `turn.completed` usage.
- An admission probe, keepalive or heal turn that ends `ok`.
- An attempt whose classification records `model_answered`, or that ends `ok`
  where the adapter records none.

`system/init` does not count: every refused attempt on 2026-09-30 had one. The
daemon reads each live detached attempt's stream for this as the attempt worker
passes it: at most once a second, 256 KiB at a time, regular files only, and
never after the first answer.

**Why an answer, and why 900 s.** Nothing cheaper shows it. claude-5's usage
readings looked healthy while its organisation had turned Claude Code off, and
the profile endpoint answered too. A model answering is the only proof, so the
window bounds how long the last proof is trusted. The request suggested the
probe interval or longer. 900 s is fifteen of C-18.1's 60 s probe intervals:

- A lane in use proves itself with each attempt it starts, and never waits.
- A lane idle that long costs one serialized start: the pilot's time to a first
  answer. That is seconds normally, and one to three minutes at the incident's
  load.
- It costs nothing while any other lane can take the work.

A shorter window gates lanes in light use for no gain. A longer one trusts an
older proof. The incident's lane had been idle for at least 6,742 s, well past
either.

**Why "answered", not "placed".** The request was framed as "a lane that has
placed nothing for a long time". Placement is no proof: the incident placed 37.
Time since the last answer covers placement idleness, and it also catches a
lane that kept taking work that never answered.

**How it meets PR #72.** PR #72 added the load band (`admission.lane_spread`,
C-11.3) and the uncapped C-6.9 rules. The band ranks each candidate by its
attempts in flight divided by the spread, so lanes fill evenly. It does not
stop the only lane above its floor taking every job, and that is what happened.
The pilot hold is a refusal before the ranking. The band orders what is left,
and a proven lane rejoins at its band.

The hold reuses the probe-lease mechanism (C-11.4): a slot block in
`unavailable_lanes`, judged `no-slot`. So it inherits C-6.9's uncapped rule (it
holds nobody back), C-6.10's backoff, and C-11.8's "room, not a standing
refusal". The early view and the reservation's check (C-6.3) lay the marks with
one pure function, `capacity.pilot_marks`. A pilot placed between the two, or an
answer heard between them, changes that lane, and the lane is judged again.

## Invariants (each executed by a property or differential test)

1. Lane-fault safety. An `auth-dead` attempt leads to another attempt only when
   it is a lane fault. A pinned job, a turn, or a job whose workspace changed
   ends `failed` with rc 5 (`tests/fake/test_lane_fault.py`).
2. Your property. No unpinned job with an unchanged workspace ends `failed`
   because of `auth-dead` while another enabled lane exists. With a live lane it
   succeeds; with none it waits. This holds for random fleets, dead lanes,
   pins, writable and read-only jobs, completion orders, and the pilot hold on
   or off (Hypothesis). With the lane-fault path switched off, the property
   fails.
3. Boundedness. Each lane fault disabled its lane. No job has more lane faults
   than there are lanes. Attempts other than lane faults never exceed
   `max_attempts`.
4. The pilot invariant. A pass never places an attempt on an unproven lane that
   already had an unanswered detached attempt in flight, and places at most one
   on an unproven lane that had none. No job is held `lane-proving` unless a
   pilot is in flight. Once every remaining lane has answered, nothing is left
   held. This is checked over random orders of submissions, passes, answers,
   ends, dead lanes and aging (Hypothesis). With no marks, it fails.
5. C-6.3 agreement (differential). After a pilot is placed, or an answer is
   heard, between a job's early evaluation and its reservation, the check
   decides what a fresh evaluation decides, verdict for verdict. With the marks
   left out of the check, this fails.
6. The incremental reader agrees with parsing the whole stream, for any write
   splits and chunk sizes (Hypothesis).
7. `capacity.pilot_marks` is order-independent, and marks exactly the cold
   lanes with an unanswered live detached attempt (Hypothesis). The Claude
   predicate never raises, and it never counts a placeholder (Hypothesis).

## Where the hold shows, and where it does not

- `why`, `run --dry-run` and `subfleet pick` all evaluate on
  `Daemon._capacity_view`, so they see a pilot's block as admission does. A
  held job's hold reads `lane-proving`, and its rejection's `slot_block` names
  the pilot.
- `status.json` (C-18.1) is built from the timers' snapshot, not
  `_capacity_view`, so the app does not show a lane being proven. That is left
  for a follow-up.
