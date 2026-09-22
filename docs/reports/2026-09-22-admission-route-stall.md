# Admission stall on email pins, 2026-09-22

## What happened

Between about 11:40 and 14:05 EDT on September 22 no session could get a job
placed. `daemon.log` repeated `worker admission failed: ValueError` up to
`(128 in a row, next try in 60 s)`. The store agrees: no attempt was reserved
between 14:45:05Z and 18:02:45Z (198 minutes). `subfleet why` looked healthy:
each held job printed `No decision recorded.`. Two shorter bursts earlier that
day each reached sixteen in a row; the first was reported at 09:41 EDT.

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

`_admit_pass -> _prepare_route -> _pick -> scheduler.evaluate` caught nothing
per job, so the whole pass aborted at the first of the five on every tick.
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
     have given.
   - Any other error waits on `route`, never terminally, with backoff from 5 s
     to 300 s. It holds no other job back, and `why` and `status` name the
     error. Failing on these errors could fail the whole queue at once, for
     example on one malformed closure or after a policy edit, so they wait.
2. **Pins become lane ids at submit (C-11.2).** Submit resolves the pin
   against the capacity view's lanes, the same lanes admission uses, and stores
   the lane id. No later roster change can make an accepted pin ambiguous. The
   pin as the caller typed it stays in the request digest, so retries remain
   idempotent. It also stays in the `job.submitted` event.
3. **One resolver, narrowed by provider (C-11.2).** A pinned job evaluates one
   model, so a name that two providers share is narrowed to the provider of
   that model: the pinned model, or else the first model of the task's chain
   from its tier. Submit, `evaluate`, admission's C-6.9 lane demand, and the
   restart repair all call the same `resolve_lane` with that provider. A
   disabled binding is also dropped when an enabled lane that matches the same
   name holds the same credential (a re-enrolment). A name that still matches
   several lanes is refused at submit, and the refusal names the lane ids.
4. **Existing queue.** Each daemon start rewrites the pin of every unfinished
   job whose name resolves to one lane, and records a `job.pin_canonicalized`
   event. It leaves any other pin in place with a log line. `subfleet doctor`
   fails its `unfinished jobs pin lanes by id` row while any job carries such
   a pin.

## Verification

`tests/fake/test_admission_route_isolation.py`, 33 cases, run against the
in-process fake daemon. On `origin/main` 0969762 the incident cases fail as
production failed. `_admit` raises
`ValueError: pinned_lane: ambiguous lane 'max@example.invalid'; use a lane id`,
and the worker-pool case logs
`worker admission failed: ValueError (1 in a row, next try in 0.5 s)`. With the
fix every case passes, and so does the full suite.
