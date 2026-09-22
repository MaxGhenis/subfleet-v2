# Admission ValueError, 2026-09-22: forensics

`daemon.log` recorded `worker admission failed: ValueError` three times that day: two streaks
that reached `(16 in a row, next try in 60 s)` and one that reached `(128 in a row ...)`.
The worker loop logs only the exception's type (C-5.10), so the log named no cause. This
report reconstructs the cause from the store and reproduces it. It checks the account in
`docs/reports/2026-09-22-admission-route-stall.md` (PR #25) independently.

Evidence labels: OBSERVED means computed from data or run; READ means found in code at the
named revision.

## Sources

- The store as of 18:21Z. The opus-5-5 cutover wrote it with `VACUUM INTO` while the daemon
  was stopped (`~/.subfleet/cutovers/opus-5-5-20260922T182100Z/state-before.sqlite3`). It was
  read from a copy through an immutable read-only URI.
- `daemon.log`, whose lines carry no timestamps. Times come from `attempts.reserved_at`,
  `jobs`, and `events`.
- Code at 0969762, the release that ran from about 13:35Z to 18:21Z
  (`releases/20260922T133117Z`), and at 605d574 (running since 18:21Z).

## Cause

1. **Five dispatch jobs were submitted with email pins.** OBSERVED: each job's
   `payload_digest` (C-6.2) can be recomputed from its prompt file. The digest matches only
   with the pin `max@axiom.org` (autumn-budget-plan-r2-axiom, lineage-proposal-review-r2,
   pr163-launcher-review) or `max@thesisinstitute.org` (microdf-fail-closed,
   pepy-microdf-estimators). All five have a task, tier `standard`, and no model.
2. **Submit accepted those pins as unique.** READ, 0969762 `daemon.py:739-745`: submit
   resolves the pin against `store.lane_rows()` and stores the pin exactly as typed unless
   the job carries an unmeasured-reserve reason. A Codex row there has no email, so each
   address matched one Claude lane. OBSERVED: `resolve_lane(lane_rows, pin)` returns
   `claude-11` and `claude-9`.
3. **Admission found each pin matching two lanes.** READ: `_pick` evaluates against
   `_capacity_view`, and `Timers.enrich_view` updates each lane row with its latest
   non-empty `timer.verdict` metadata. `scheduler._identities` includes that `email`.
   OBSERVED: the Codex usage probe had reported an email for every Codex lane since
   2026-09-19. Each one is also a Claude lane's account:
   codex-1 max@maxghenis.com, codex-2 mghenis@gmail.com, codex-3 max@axiom.org,
   codex-4 max@policyengine.org, codex-5 max@thesisinstitute.org, codex-6 max@ubicenter.org.
4. **One raise aborted the whole pass.** OBSERVED, by rebuilding the view at 15:40Z with
   `capacity.build_view` plus the verdict metadata and running 0969762's
   `scheduler.evaluate` over the queue in admission order:
   `ValueError: pinned_lane: ambiguous lane 'max@axiom.org'; use a lane id`, raised at
   `scheduler.py:56 in resolve_lane`, on the first job in line. READ: `_admit_pass ->
   _prepare_route -> _pick -> scheduler.evaluate -> resolve_lane` has no per-job handler,
   so every later job in every tier went unplaced and C-5.10 paced the retries.

## Timeline (UTC)

| Time | What | Source |
|---|---|---|
| 13:24:34 | Gate round `...pr-1d23e21c-r1` submitted, `fable` pinned to `max@axiom.org` | jobs |
| 13:24:34 to about 13:35 | First streak: every pass raises on it (0969762 does not narrow a pin by the model's provider) | reproduction at 13:28Z |
| about 13:35 | Release 20260922T133117Z installed (lane-pins cutover); restart forgives the count | cutovers, release.json |
| about 13:35 to 13:48:53 | Second streak, same job, ends when it is cancelled; round r2 is pinned to another account | jobs |
| 14:33:03 | autumn-budget-plan-r2-axiom submitted (`-a max@axiom.org`) | jobs, digest |
| 14:33 to 14:45 | Not yet evaluated. The older standard Opus job thesis-prepush-guard-review is a capacity waiter (ten unplaced decisions, 14:23 to 14:43), so C-6.9 holds autumn-budget `behind-older-job`, as it holds nssf-codify-pilot-opus, before either route is prepared. A Fable gate round is placed at 14:44:49 because it does not compete | decisions, attempts, READ `_admit_pass` |
| 14:45:05 | thesis-prepush and nssf are placed. autumn-budget is first in line, and every pass from here raises | attempts |
| 14:45:05 to 18:02:45 | Third streak (reaches 128): no attempt reserved for any job, 198 minutes | attempts |
| before 18:02:36 | The five pins are rewritten in the store to `claude-11` and `claude-9` | pins in the 18:21Z copy differ from the digests |
| 18:02:45 | autumn-budget placed on claude-11 after its probe at 18:02:36; placing resumes | attempts, events |
| 18:21 | Controlled restart into 605d574 | cutover |

The streak lengths fit C-5.10's backoff. Sixteen consecutive failures take about ten
minutes and 128 take about two hours, and no window was long enough for the next power of two.

## What ended it

The out-of-band store edit ended it, not the restart. The 18:21Z copy stores lane ids for all
five jobs, while their digests prove they were submitted as emails, and 0969762 stores the pin
as typed. The first attempt after the stall is autumn-budget itself, at 18:02:45Z, which is 18
minutes before the restart. PR #25 attributes the edit to a Microcosm session. That
attribution is not checked here.

## Still live on 605d574

The restart installed only PR #24, the Opus model id. READ, 605d574: `resolve_lane` and
submit's resolution are unchanged, and `_admit_pass` still isolates nothing per job. Any new
job pinned by one of the six shared emails is accepted and stalls admission again. A
`--dry-run` answers `invalid arguments: pinned_lane: ambiguous lane 'max@axiom.org'` because
it evaluates against the capacity view, but a real submit does not evaluate that way. Until
PR #25 is deployed, pin by lane id (`-a claude-11`).

## Against PR #25's account

- Its cause is confirmed: the five jobs, the two resolutions, the abort, and resumption at
  18:02:45Z. Its provider narrowing also covers the earlier gate round, which carried
  `-m fable`.
- Its report places the stall "between about 11:40 and 14:05 EDT". The store puts the start
  at 14:45:05Z, which is 10:45 EDT, as the same report's own figure says.
- It does not explain the two earlier streaks. They were the `max@axiom.org` gate round above,
  on either side of the 13:35Z install.
- C-6.9 delayed the onset by twelve minutes, because a job held `behind-older-job` is never
  evaluated. The same hold can shield or expose a bad job depending on what waits ahead of
  it.

## Why the log could not say this

The only lines were `worker admission failed: ValueError (N in a row, next try in 60 s)`.
With C-5.10 as amended here, the first failure of a streak also carries the traceback without
its message. On this incident that traceback would have ended in
`File ".../subfleet/scheduler.py", line 56, in resolve_lane`, below
`_admit_pass -> _prepare_route -> _pick -> evaluate`.
