# Admission stall on email pins, 2026-09-22

## What happened

On September 22 no job was placed from 10:45 to 14:03 EDT: the store has no
attempt reserved between 14:45:05Z and 18:02:45Z (198 minutes). Each pass still
placed the jobs ahead of the stuck one in admission order and stopped at it.
Every job pending in that window was a standard-tier job submitted after it, or
a hard-tier job, so all of them waited. The stall was noticed around 11:40 EDT. `daemon.log` repeated
`worker admission failed: ValueError` up to `(128 in a row, next try in 60 s)`.
`subfleet why` pointed nowhere: the five jobs that caused it printed
`No decision recorded.`, and every other held job showed an ordinary decision,
either one recorded before the stall (for example
`20260922-092535-corpus728-base-layer-plan`) or one evaluated for the answer.

The same resolution had stalled admission once already that day, from 09:24 to
09:50 EDT. No attempt was reserved from 13:24:08Z to 13:50:32Z. The cause was
gate review `20260922-092434-gate-20260922-132433-pr-1d23e21c-r1`, pinned
`-a max@axiom.org -m fable` and submitted at 13:24:34Z. It held every pass until
it was cancelled at 13:48:53Z. `daemon.log` shows the failure count reaching
sixteen twice only because the lane-pins cutover restarted the daemon at
13:35:39Z. The provider narrowing below resolves that pin as well: `fable` runs
on Claude.

The first stuck job, `20260922-103303-autumn-budget-plan-r2-axiom`, was
submitted at 14:33:03Z behind two older jobs. Those two were reserved at
14:45:05Z, and every pass after that stopped at it.

## Cause

Five queued dispatch jobs were pinned by email, `-a max@axiom.org` or
`-a max@thesisinstitute.org`, with a task (`review` or `build`, tier
`standard`) and no model:

- `20260922-103303-autumn-budget-plan-r2-axiom`
- `20260922-113721-lineage-proposal-review-r2`
- `20260922-114521-microdf-fail-closed`
- `20260922-114522-pepy-microdf-estimators`
- `20260922-124556-pr163-launcher-review`

Submit and admission resolved those names against different lane data:

- Submit called `scheduler.resolve_lane(self.store.lane_rows(), pin)`. In the
  `lanes` table a Codex lane has an account key `codex:<uuid>` and no label, so
  the email matched only the Claude lane (`claude-11`, `claude-9`). The job was
  accepted, and it kept the raw email as its pin, because only jobs carrying an
  unmeasured-reserve authorization were stored with the lane id.
- Admission called `scheduler.evaluate` on the capacity view. There,
  `Timers.enrich_view` merges each lane's latest `timer.verdict` metadata, and
  the Codex usage probe records the account's `email`. The same address named
  `claude-11` and `codex-3`, and `claude-9` and `codex-5`.
  `resolve_lane` raised `ValueError("pinned_lane: ambiguous lane ...")`.

The Codex emails had been in the view since 2026-09-19. Nothing changed after
submit: the two sides of the daemon disagreed from the moment each job was
accepted.

`_admit_pass -> _prepare_route -> _pick -> scheduler.evaluate` caught nothing
per job, so every pass aborted at the first of the five.
Every later job in every tier waited, and C-5.10 paced and logged the failure.
`_why_job` caught the same `ValueError` and printed `No decision recorded.`.

A Microcosm session unblocked the queue by rewriting the five pins directly
in the store (`claude-11` for the three axiom.org jobs, `claude-9` for the two
thesisinstitute.org jobs). Placement resumed at 18:02:45Z.

This was reproduced before the fix by fetching the live capacity view
(`client.Client().call("daemon.status", {})`) and running
`scheduler.evaluate(policy, view, job)` for each queued job. It is reproduced
again below with a fake daemon.

## Fix

Contract clauses C-6.12 (new) and C-11.2, with C-4.1's `route` wait and
C-6.11's `route` hold.

1. **One job never stops the pass (C-6.12).** Route evaluation is isolated
   per job, both where the route is prepared and inside the reserving
   transaction, which rolls back first. It covers `evaluate` and
   `probe_required`, which checks an unmeasured-reserve authorization. A
   `ValueError`, `KeyError`, or `TypeError` settles that job, and the pass
   moves on.
   - A `scheduler.RouteError` is the job's own problem: an ambiguous pin, a
     pin and a model on different providers, or an incomplete authorization.
     The job fails with exit 2 and the message, the same answer submit would
     have given. `why` prints `Refused at admission: ...`, and a worktree cut
     for the job that no attempt used is removed. A provider conflict can also
     come from a policy edit that moved a tier to another provider; that one
     is refused only under the policy the job was accepted with.
   - Any other error waits on `route`, never terminally, with backoff from 5 s
     to 300 s. It holds no other job back. `why` names the error, and
     `status` counts the hold. Failing on these errors could fail the whole
     queue at once, for example on one malformed closure or after a policy
     edit, so they wait.
2. **Pins become lane ids at submit (C-11.2).** Submit resolves the pin
   against the lanes admission uses, named as the capacity view names them:
   each lane row with its latest probe verdict merged in, as
   `Timers.enrich_view` merges it. It leaves out the readings, which cost
   about 50 ms per view on the live store. It then stores the lane id. No
   later roster change can make an accepted pin ambiguous. The pin as the
   caller typed it stays in the `job.submitted` event and, except for a job
   with an unmeasured-reserve authorization (bound to its lane id), in the
   request digest, so retries remain idempotent.
3. **One resolver, narrowed by provider (C-11.2).** A pinned job evaluates one
   model, so a name that two providers share is narrowed to the provider of
   that model: the pinned model, or else the first model of the task's chain
   from its tier. Submit, `evaluate`, admission's C-6.9 lane demand, and the
   restart repair all call the same `resolve_lane` with that provider. A
   disabled binding is also dropped when an enabled lane that matches the same
   name holds the same credential (a re-enrolment), and a pin to such a lane id
   follows it to its successor. With no model or task, the flag decides: `-a`
   names a Claude account and `-H` a Codex home, as in v1, so a bare
   `-a <email>` stays a Claude pin even though a Codex lane answers to the same
   email. A name that still matches several lanes is refused at submit, and the
   refusal names the lane ids. A retry of an accepted request is answered from
   the lane it was accepted on. A Codex lane keeps its email through a probe
   that could not read the account.
4. **Existing queue.** Each daemon start rewrites the pin of every unfinished
   job whose name resolves to one lane, and records a `job.pin_canonicalized`
   event. It leaves any other pin in place with a log line. `subfleet doctor`
   fails its `unfinished jobs pin lanes by id` row while any job carries such
   a pin.

## Verification

`tests/fake/test_admission_route_isolation.py` runs against the in-process
fake daemon. On `origin/main` 0969762 the incident cases fail as production
failed. `_admit` raises
`ValueError: pinned_lane: ambiguous lane 'max@example.invalid'; use a lane id`,
and the worker-pool case logs
`worker admission failed: ValueError (1 in a row, next try in 0.5 s)`. With the
fix every case passes, and so does the full suite.

An adversarial review (five dimension reviewers) found 31 issues in the first
version, among them:
- a policy edit could terminally fail every lane-pinned job of a tier;
- a refused writable job leaked its worktree;
- `subfleet why` still printed `No decision recorded.`;
- retries across a roster change were refused;
- a canonical pin never followed a re-enrolment;
- a Codex email disappeared after one failed probe;
- an empty tier waited forever;
- every admission pass built a full capacity view (about 50 ms live).

Each is fixed and covered by a test. For each fix, the regression the review
described was applied as a mutation, and a test caught every one of them.

A second round (four reviewers, with the live store read-only) found no live
job that the install would fail, strand, or misroute. It confirmed six more
findings, all now fixed and tested the same way:
- A transient retry pinned to a model id the policy had since renamed (opus
  moved from `claude-opus-5` to `claude-opus-5-5` the same day) would have
  waited on `route` forever. It now routes the job as submitted.
- A resume routed to a re-enrolled lane's successor was refused at launch.
  Launch now accepts the successor, because the native session lives under
  the credential's home.
- A lane whose credential proved to hold another account answered to that
  account's email. It now keeps that address as `observed_email` and is
  dropped from a name's matches.
- Two corrections to docs.

A third round found six more, all now fixed:
- The transient-retry pair is now used only while it can run: its model must
  resolve to the lane's provider, and the lane must be enabled, v2-owned and
  not identity-blocked. A lane disabled since the attempt had pinned the retry
  on main as well.
- The identity-mismatch narrowing now applies only within one provider.
- Three corrections to docs.

The production-safety reviewer in round three and the install-safety reviewer
in round two found nothing.
