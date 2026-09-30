# The desktop login as a lane with a reserve; holds only where the older job could run (2026-09-30)

Base: `release/217` at f1bd2ab5 (the line 2.1.9 is built from). The installed release, 617892c1,
predates #72.

## What happened on 2026-09-30 (read from `~/.subfleet/state.sqlite3`)

- **Every other Opus lane was out.** `subfleet why 20260930-114555-pr87-review-2` at 15:59Z:
  - claude-1, -2, -7 and -8 closed;
  - claude-3, -4, -6 and -10 `below-floor`;
  - claude-5 and claude-11 to -17 `disabled`;
  - claude-9 `desktop`;
  - every Codex lane `below-floor` or closed.
- **The desktop lane, claude-9 (max@thesisinstitute.org), sat idle.** The installed daemon excludes it unless a job says
  `--allow-desktop`. #72, on `release/217`, excludes it only while Claude Code uses the login.
  Max is nearly always using Claude Code, so under #72 the lane would still almost never
  be used. Max: "why wouldnt we allow using the active acct?"
- **Head of line.** `20260930-115551-pb-gpt61sol-rejudge3` had `allow_desktop` 1, so claude-9 would have taken it.
  `why` said it was held behind `20260930-114555-pr87-review-2`, an older standard job that has
  `allow_desktop` 0 and can never run on claude-9. The live policy's interim caps (1000) make the pool
  capped, so C-6.9's hold-back applied. `scheduler.competes` compares models and lane *pins* only.
  An unpinned job "could run on every lane", so the older job held the newer one back from a lane it
  could not use itself.
- **auth-dead.** Between 15:43:20Z and 15:44:00Z, 45 attempts were reserved on claude-5 (no caps; it was the
  only open Claude lane once its five-hour closure ended). All 45 finished `auth-dead: Your organization has
  disabled Claude subscription access for Claude Code`. The classification is right: `ORG_BLOCK_RE`
  matches, the fixture `tests/fixtures/claude/org-block` covers it, and the lane was disabled at the first finish
  (15:44:53Z), with no reservation on claude-5 after that. But `_finalize` retries only `limited`
  (unpinned), `transient` and a lost read-only attempt. So each of the 45 jobs **failed with rc 5**, although only
  the lane's credential was dead.

## Decisions

### 1. The desktop login is a lane by default, with a reserve (C-10.3, C-11.2, C-11.4, C-6.11)

- The desktop lane is a candidate for every job whether or not Claude Code is using the login. It still
  sorts after every other candidate (C-11.3, unchanged), so it is taken only when nothing else can take the job.
- **Reserve**, for detached work only. A turn is attended, the person's own use of the login (C-26.9),
  and is never refused by the reserve. `admission.desktop_reserve`, default
  `{"five_hour": 0.3, "seven_day": 0.3}`, is the headroom the login keeps for interactive sessions. The
  desktop lane is refused `desktop-reserve:<window>` while that window's **latest `provider` reading is at or
  above `1 - reserve`**. A reading keeps counting after `reading_ttl_s`, until its window's `resets_at`.
  A reading with no `resets_at` counts only while fresh. `null` for a window, or for the whole key, turns that
  part off.
  - Why a stale reading counts: the desktop lane's only readings come from the `rate_limit_event` of attempts
    that ran on it, recorded when each attempt ends. Setup-token lanes answer 403 on `/api/oauth/usage`; the
    live store shows 409 `probe|unknown` readings and no `oauth-usage` reading. The daemon has also never
    verified the desktop credential: there is no `desktop.identity` event in the store. With
    `reading_ttl_s` 120, a reserve that looked only at fresh readings would be blind about two minutes after
    each attempt. Within one `resets_at` the store's readings are non-decreasing in 54,650 of 54,698
    consecutive pairs. The 48 drops include 0.98 to 0.08 on claude-10's seven-day window, a reset inside the window.
    So an old reading can overstate usage, which only refuses more; it cannot understate usage it has seen.
    What it cannot see is usage since it was taken. The in-flight bound below covers that.
- **In-flight bound.** `admission.desktop_max_in_flight`, default 2, counts the detached attempts on the desktop lane.
  At the bound the lane is refused `desktop-reserve:in-flight`. `null` means no bound, and 0 keeps detached work off
  the login entirely. This is part of the reserve, not a queueing cap. Readings arrive only when an attempt ends,
  so without a bound one admission pass could put the whole queue on the login before any reading showed it:
  45 attempts reached claude-5 within 40 s today. 2 is the per-lane default before the uncap
  (`docs/plan-b-rev4.md`: `max_in_flight_per_lane` 2). It is not one of C-6.9's pool caps, so it creates no hold-back.
- **`--no-desktop`** (and `-x @desktop`, which it spells) keeps a job off whichever lane is the desktop login when
  the job is placed. `@desktop` is a reserved exclusion token that no lane identity can equal. It rides
  in `exclusions`, so there is no schema change and old requests keep their digests. It is carried through
  retries, resumes, `why` and batch manifests like any exclusion, and the refusal is the ordinary `excluded`.
  `--allow-desktop` stays accepted and is a no-op. The reserve applies whatever a job says, because agents pass
  that flag routinely (rejudge3 did). Giving both flags is exit 2.
- The in-use signal (`desktop_in_use`, the registry read, `desktop.in_use` events) stays computed and published.
  C-6.9's priority classes use the same registry read. It no longer refuses anything: not admission, not the
  admission-probe reservation, not `open_lanes`, not `status.json`'s `dispatchable`. Those now follow the reserve.
- C-6.3: a reading counted past its freshness stops counting at its `resets_at`. `capacity.lane_horizons` adds that
  instant for the desktop lane, so the check inside a reservation judges the lane again when the reserve lifts.
  The in-flight count is already a changed-lane trigger (`route_check.in_flight`).

### 2. An older job holds a newer one back only where it could run (C-6.9)

- **Rule.** In a pool with a count cap, a newer job J is held behind an older waiting job W of its tier and scope
  only when W could be placed on the lane J would take. W's `judge_lane` on that lane, for a model of W's chain,
  must give no reason other than `no-slot` or `desktop-reserve:in-flight`, the slot J would take.
  Otherwise J is not held by W.
- **Cheap where nothing differs.** Each pass computes, per job, its *usable lanes*: the lanes that job-specific
  facts allow. These are the chain's providers, the pin, the exclusions (with `@desktop` on the desktop lane), and a
  turn's `config-dir`. Suppose W's usable lanes contain J's and W's demand models contain J's. Then whatever
  lane and model J would take, only facts the two jobs share decide whether W may take it too, and J is held as
  today, with no evaluation. That covers every pair of jobs submitted the same way.
- **Otherwise one evaluation.** J's lane is `scheduler.evaluate` on a capacity view shared by the pass and
  reused for up to 2 s (`HOLD_VIEW_TTL_S`; a view costs about 50 ms on the live store, `_pin_roster`'s
  docstring). J with no lane at all is held as today. The check is made again on J's own decision inside the
  reservation, so a lane that opened since the shared view cannot let J pass W there.
- The kept last slot (`slot-kept`) and lease FIFO are unchanged. A waiter blocked on a lease J holds still
  never holds J (#72).

### 3. An auth-dead attempt closes its lane and the job moves on (C-9.3, C-23.44, C-4.5)

- An `auth-dead` attempt of a job not pinned to a lane is retried like a `limited` one: the job waits on capacity,
  and it counts toward `max_attempts`. The lane is disabled in the same transaction, as before, so the retry routes
  elsewhere. A pinned job still fails with rc 5, and a job out of attempts too.
- Classification is unchanged. `ORG_BLOCK_RE` covers "Your organization has disabled Claude subscription access
  for Claude Code". A regression test fixes the exact 2026-09-30 message.

## Invariants (each with a property or example test)

1. **Desktop eligibility.** For any view and job, the desktop lane's refusal reasons contain none that the in-use
   signal alone decides: flipping `desktop_in_use` never changes a decision.
2. **Reserve soundness.** A detached job is never placed on the desktop lane:
   - while a counted reading of either window is at or above `1 - reserve`;
   - or while `desktop_max_in_flight` detached attempts are on it.
   A turn is never refused by either.
3. **Monotone in the reserve.** Raising a window's reserve, or lowering the bound, never places a job on the
   desktop lane that it would not have placed before.
4. **`--no-desktop`.** A job carrying `@desktop` is never placed on the lane the view marks desktop, whatever
   its other flags. A job without it is refused that lane only by reasons that job carries too.
5. **Hold soundness (the fix).** No job is held `behind-older-job` behind a waiter that `judge_lane` refuses, on the
   lane the job would take, for a reason other than the slot. (a) in the requested tests.
6. **Fairness kept.** When the older waiter could run on the lane the newer job would take, the newer job is held,
   exactly as before. (b) in the requested tests. Over identical jobs, the new rule and the old one hold the same jobs.
7. **Differential.** `scheduler.evaluate` equals `tests/reference_scheduler.py` with the reserve. The reference
   implements it separately.
8. **auth-dead.** An unpinned job whose attempt is auth-dead is never failed while it has attempts left, and its
   lane takes no attempt after that finish.
