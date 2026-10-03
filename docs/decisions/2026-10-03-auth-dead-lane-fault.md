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
  or it is writable, its model never answered, and its salvage at finalization
  found the end tree equal to the attempt's start snapshot (no ref written,
  nothing left out, no error) with HEAD where the attempt began;
- it is the job's first lane fault.

On a lane fault, the job moves on to the next candidate as it would after
`limited`. The lane is disabled as before (C-23.44). The attempt records
`lane_fault` in its evidence, `daemon.log` says the job moved on, and the job's
eventual notice names each lane it moved on from. A pinned job, or one whose
workspace changed, keeps today's behaviour: it ends `failed` with rc 5 for
reconciliation.

**One more attempt, once.** The brief offered two options: not count the
attempt, or grant one more. The rule grants one more attempt, once per job. The
lane fault is not counted against `max_attempts`, and a job has at most one.

I first built "not counted, bounded by the number of lanes", on the reasoning
that each lane fault disables its lane. The independent review of PR #121 showed
why that bound is the wrong one. `auth-dead` can be caused by the job, not the
lane: a tool that prints `invalid_api_key` to Codex's stderr, or a project
setting that overrides the lane's credential (writable Claude launches read
project settings). Such a job would move from lane to lane and disable every
one, and each then needs `subfleet lanes enroll`. Before this change that job
disabled one lane and failed.

So a second `auth-dead` on a job that already moved on ends the job (`failed`,
rc 5) and **leaves that lane enabled** (C-23.44). Two lanes refusing one job
points at the job. The guarantee is that no job disables more lanes than before
this change: at most one. A lane that really is dead is disabled by the next
job that meets it, whose first `auth-dead` it is, and that job moves on.

**A job that moved on is no pilot.** Round 2 of the review found what "leave the
second lane enabled" costs when two lanes are dead: each moved-on job that
reaches the second lane as its pilot fails there, the lane stays enabled, and the
next moved-on job does the same, so a whole burst can fail one job at a time.
The fix is the reviewer's: a moved-on job never tries an unproven lane itself.
It waits for Subfleet's own admission probe of that lane (C-11.4: a fixed
prompt, on the lane's credential, with none of the job's settings). The probe's
`auth-dead` is the lane's and disables it; its answer proves the lane. So a
moved-on job meets `auth-dead` again only on a lane a model answered on within
`prove_idle_s`, which does point at the job, and a second dead lane is found by
a probe, not by failing jobs. With the hold on (the default), the brief's
property holds whole: no unpinned job with an unchanged workspace fails for
`auth-dead` while another lane can run it. With the hold off there is no proof
to ask for, and the earlier cost stands: a job that meets two dead lanes fails.

**A job allowed one attempt still moves on once.** A writable job's lane fault
ran nothing the job asked for, since no model answered. A revive sets
`max_attempts` 1 because "a retry would be a second continuation", and an
attempt no model answered is not a first one, though its prompt and Claude
Code's placeholder may be left in the revived session's transcript, which the
retry continues from. A read-only job's lane fault repeats only reading.

**Why a writable job also needs "the model never answered".** The brief's test
was the tree: read-only, or end tree equal to the start snapshot. The review
pointed out what the tree cannot show. A writable Claude attempt runs with
permissions skipped, so its model can push, comment on a PR, or write outside
the worktree. If access is revoked after that, the tree and HEAD are unchanged,
and a retry would repeat those effects. So a writable job is a lane fault only
when its attempt's evidence records `model_answered` false; where an adapter
records nothing, or read no event of the stream (`model_answered` null), it is
taken to have answered. The incident's writable jobs never got an answer, so
they qualify. A read-only job moves on whether or not its model answered, as the
brief asked: reading again repeats nothing.

**A prerequisite: `auth-dead` only from the CLI's own words (C-9.3).** The
Claude classifier matched its organisation-block and credential phrases
against every assistant frame, the model's included. A review of this very
classifier quotes "Your organization has disabled Claude subscription access",
and that disabled a lane. Now only these count as evidence: stderr, the frames
that are the CLI's own (its placeholders), the result's `errors`, and its text
when it is marked `is_error`. A test pins that a model quoting the phrases ends
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

A pilot that has not answered after `admission.prove_wait_s` (300 s; null
waits however long) stops holding the lane, which then takes one more attempt
as the next pilot. Without this, a pilot that hangs before its first answer
would hold its lane until `max_wall_s` (the review's finding). Five minutes,
because the incident's refusals took 78 to 181 s to arrive under its load.

**What counts as an answer.** The first non-synthetic assistant event, or usage:

- Claude: an `assistant` frame a model served (and that the CLI stamped with no
  error kind), a non-error `result` whose usage counts a token, or
  `system/thinking_tokens` with a positive count. A real 2026-10-03 stream
  (`20261003-101520-mpv-mg-openmessage`) shows Claude Code writing these while
  the model thinks, before the first assistant frame, so a long-thinking pilot
  proves its lane early.
- Codex: a thread item only the model produces, or `turn.completed` usage.
- An admission probe, keepalive or heal turn that ends `ok`.
- An attempt whose classification records `model_answered`, or whose adapter's
  verdict is `ok`.

When in doubt the predicate says no: a lane read as proven takes a burst.

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

The limit of the rule: a lane whose access goes within the window of its last
answer is still taken as proven, and can take a burst. Each of those jobs then
moves on once (call 1).

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
2. The brief's property. With the pilot hold on, no unpinned job with an
   unchanged workspace ends `failed` because of `auth-dead` while another enabled
   lane exists, however many lanes are dead: it moves on, then succeeds on a
   live lane or waits if there is none. With the hold off it holds for the first
   dead lane a job meets, so for the incident's shape; a job that meets a second
   ends `failed` there and leaves that lane enabled. Only moved-on jobs wait for
   probes, and only with the hold on. This holds for random fleets, dead lanes,
   pins, writable and read-only jobs, completion orders, and the hold on or off
   (Hypothesis). With the lane-fault path switched off, the property fails (a
   checked-in mutation test).
3. No amplification. No job disables more than one lane. No lane that works is
   ever disabled. A job has at most one lane fault, and its other attempts
   never exceed `max_attempts`. With lane faults uncounted, the property fails
   (a checked-in mutation test).
4. The pilot invariant. A pass never places an attempt on an unproven lane that
   already had an unanswered detached attempt in flight, and places at most one
   on an unproven lane that had none. No job is held `lane-proving` unless a
   pilot is in flight. Once every remaining lane has answered, nothing is left
   held. This is checked over random orders of submissions, passes, answers,
   ends, dead lanes and aging (Hypothesis). With no marks, it fails (a
   checked-in mutation test).
5. C-6.3 agreement (differential). After a pilot is placed, or an answer is
   heard, between a job's early evaluation and its reservation, the check
   decides what a fresh evaluation decides, verdict for verdict. With the marks
   left out of the check, this fails (a checked-in mutation test).
6. The incremental reader agrees with parsing the whole stream, for any write
   splits and chunk sizes (Hypothesis).
7. `capacity.pilot_marks` is order-independent, and marks exactly the cold
   lanes with an unanswered live detached attempt reserved within the wait
   (Hypothesis). The Claude
   predicate never raises, and it never counts a placeholder (Hypothesis).

## Where the hold shows, and where it does not

- `why`, `run --dry-run` and `subfleet pick` all evaluate on
  `Daemon._capacity_view`, so they see a pilot's block as admission does. A
  held job's hold reads `lane-proving`, and its rejection's `slot_block` names
  the pilot.
- `status.json` (C-18.1) is built from the timers' snapshot, not
  `_capacity_view`, so the app does not show a lane being proven. That is left
  for a follow-up.

## The review, and what it changed

An independent Opus review of PR #121 (Subfleet run
`20261003-143159-pr121-review`) asked for changes. Taken:

- One lane fault per job, and a second `auth-dead` leaves its lane enabled
  (its finding 1).
- A writable job needs `model_answered` false (finding 2).
- `admission.prove_wait_s` bounds a silent pilot's hold (finding 3).
- An `ok` verdict always proves its lane (finding 4).
- A frame carrying an error kind, and an error result's usage, are no answer
  (finding 5).
- Only an answer on an unproven lane wakes admission (finding 6).
- The contract's `open_lanes` and `no-slot` wording, and the rule's limit
  (finding 7).
- Tests for a replayed finalization, `_unlaunched` after a lane fault, an
  admission-cut worktree, mixed counting, and checked-in mutation tests
  (finding 8).

Not taken: seeding the fake harness's lanes as proven so every fake test runs
with the hold on. The harness builds its state root before any daemon or store
exists, its tests add lanes as they go, and it already starts C-11.7's reserve
off for the same reason. The hold has its own tests with it on.

Round 2 (Subfleet run `20261003-152201-pr121-review-r2`, on `036bf377`) found
every round-1 finding resolved and asked for one more change: with two dead
lanes, moved-on jobs failed one at a time at the second. Taken, as above (a
moved-on job is no pilot). Also taken: `model_answered` is null when no stream
event was read, so stderr alone cannot make a writable job's lane fault; a
lapsed pilot wakes admission; and the revive transcript note. Left as they
are: a cancelled job's second `auth-dead` also leaves its lane enabled, and the
notice line says "two lanes" even when the second is the first lane's
re-enrolled successor.
