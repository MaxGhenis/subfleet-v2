# The desktop login as a lane with a reserve; holds only where the older job could run (2026-09-30)

Base: `release/217` at f1bd2ab5, the line 2.1.9 is built from. The installed release, 617892c1, predates #72.
This is revision 2. An adversarial design review had three lenses: fairness, the reserve, and auth-dead. Its
findings are folded in; "Changes since revision 1" lists them.

## What happened on 2026-09-30 (read from `~/.subfleet/state.sqlite3`)

- **Every other Opus lane was out.** `subfleet why 20260930-114555-pr87-review-2` at 15:59Z:
  - claude-1, -2, -7 and -8 were closed;
  - claude-3, -4, -6 and -10 were `below-floor`;
  - claude-5 and claude-11 to -17 were `disabled`;
  - claude-9 was `desktop`;
  - every Codex lane was `below-floor` or closed.
- **The desktop lane, claude-9 (max@thesisinstitute.org), sat idle.** The installed daemon excludes it unless a job
  says `--allow-desktop`. #72, on `release/217`, excludes it only while Claude Code uses the login. Max nearly always
  has Claude Code open, so the lane would still hardly ever be used. Max: "why wouldnt we allow using the active acct?"
- **Head of line.** `20260930-115551-pb-gpt61sol-rejudge3` had `allow_desktop` 1, so claude-9 would have taken it.
  `why` said it was held behind `20260930-114555-pr87-review-2`, an older standard job with `allow_desktop` 0 that
  could never run on claude-9. The live policy's interim caps (1000) make the pool capped, so C-6.9's hold-back
  applied. `scheduler.competes` compares models and lane *pins*, so an unpinned job "could run on every lane".
- **auth-dead.** Between 15:43:20Z and 15:44:00Z, 37 attempts were reserved on claude-5 for 37 jobs, 24 of them
  read-only and 13 writable. It had no caps, and it was the only open Claude lane once its five-hour closure ended.
  All 37 finished `auth-dead: Your organization has disabled Claude subscription access for Claude Code`.
  - The classification is right: `ORG_BLOCK_RE` matches it, and the fixture `tests/fixtures/claude/org-block` covers it.
  - The lane was disabled at the first finish (15:44:53Z), and nothing was reserved on claude-5 after that.
  - But `_finalize` retries only unpinned `limited` attempts, `transient` ones, and a lost read-only one. So every
    job failed with rc 5, although only the lane's credential was dead.

## Decisions

### 1. The desktop login is a lane by default, the chain's last resort, behind a reserve (C-10.3, C-11.2, C-11.4)

- **A candidate whatever Claude Code is doing.** The in-use signal stays computed and on record: C-6.9's classes read
  the same registry, and `desktop.in_use` events still explain placements. It no longer refuses anything, whether
  in admission, in the probe reservation, in `open_lanes` or in `status.json`.
- **The chain's last resort.** The lane sorts after every other candidate of its model (C-11.3), and the walk also
  goes past a model whose only candidate is the desktop lane. So a review whose Opus lanes are all out promotes to
  an open Codex lane before it spends Max's login. Only when no model of the chain has another candidate does the
  first model that has the desktop lane take it. `route_check.still_stands` and `tests/reference_scheduler.py` walk
  the same way.
- **Reserve, for detached work only.** A turn is Max's own use through the Subfleet app (C-26.9), so the reserve never
  refuses one.
  - `admission.desktop_reserve` defaults to `{"five_hour": 0.3, "seven_day": 0.3}`. `null`, for a window or for the
    whole key, keeps nothing.
  - Each window counts the latest `provider` reading for the account and for the job's model. An Opus bucket at the
    ceiling refuses Opus work; a Fable bucket does not.
  - A reading counts whether or not it is fresh, until its window's `resets_at`. One with no `resets_at` counts only
    while fresh.
  - A counted reading at or above `1 - reserve` refuses the lane (`desktop-reserve:<window>`) for
    `DESKTOP_RESERVE_REPROBE_S` (3600 s) after it was taken. After that it asks for a probe instead, so a window the
    provider reset early is found out within the hour.
- **Fresh evidence before placement.** Otherwise the lane is a candidate. `probe_required` makes detached work probe
  it first unless each reserved window has an account reading younger than `reading_ttl_s`. The probe's
  `rate_limit_event` provides that reading. The reason is that the lane's readings come only from attempts and probes
  run there:
  - its setup token gets 403 from the usage endpoint;
  - the store has no `oauth-usage` reading and no `desktop.identity` event;
  - Claude Code's own use of the login appears in none of them.

  Within one `resets_at`, the store's readings are non-decreasing in 54,650 of 54,698 consecutive pairs. The 48
  drops follow a reset inside the window; claude-10's seven-day window went from 0.98 to 0.08. So a kept reading
  overstates use, which refuses more, far more often than it understates it.
- **In-flight bound.** `admission.desktop_max_in_flight` defaults to 2 detached attempts. `null` means no bound, and 0
  keeps detached work off the login. The bound is part of the reserve, not a C-6.4 count cap, so it creates no hold-back.
  `dominant_rejection`, the idle log's expected holds and `_retry_waits_on_a_slot` treat it as room
  (`scheduler.ROOM_REASONS`). With the fresh-evidence probe, 2 is defensible: every placement is judged on a reading
  at most two minutes old.
- **`--no-desktop` is `-x @desktop`.** `DESKTOP_EXCLUSION` is a reserved token: no lane id, account key, label or
  email begins with `@`. It rides in `exclusions`, so there is no schema change and every existing digest keeps its
  value. The refusal is the ordinary `excluded`. `--allow-desktop` is accepted and does nothing: agents pass it
  routinely (rejudge3 did), and the reserve is Max's. Passing both flags is exit 2.
- `subfleet pick` always adds `@desktop`. Its callers run outside supervision, where the bound cannot count them.
- `desktop_login` accepts `"reserve"`, the shipped value, and still reads `"never"`, which had decided nothing since #72.
- If a pass cannot read the `~/.claude.json` hint, the daemon keeps the last hint it read. It never falls back to the
  recorded `lanes.desktop` flag, which the live store has on claude-1 (set at import) rather than on claude-9.

### 2. An older job holds a newer one back only where it could run (C-6.9)

- **Rule.** In a pool with a count cap, a newer job J is held behind an older competing waiter W of its tier only
  when W could take the model on the lane that J would take.
  - J's decision shows that lane admits that model now.
  - Every reason `judge_lane` gives other than the job's own facts belongs to the lane, the model or the pool. Those
    are the same for W and J.
  - So the question is W's own facts alone: `usable_pairs`, the (model, lane) pairs of W's `prepare` chain, taken
    after pin truncation and after `model_lanes` (provider and pin) and `job_refusals` (exclusions, `@desktop`, a
    turn's config directory). W's retry exclusions are included.
  - Revision 1 judged W with `judge_lane`. That was wrong: `judge_lane` checks neither a pin nor a provider.
- **Cheap where nothing differs.** If W's pairs contain all of J's, J is held with no evaluation, as before. This
  covers every pair of jobs submitted the same way.
- **Otherwise one evaluation.** J's lane comes from `scheduler.evaluate` on a view shared for `HOLD_VIEW_TTL_S` (2 s).
  It is memoized per job per view, made only for a job that is due, and built with the cached desktop answer, so it
  never reads the registry in a turn pass. The check runs again on J's own decision in two places:
  - before J's admission probe reserves a lane (`_HeldBehind`), so no probe is spent and no `slot:0` taken for a job
    that would then wait;
  - inside the reservation.

  A hold made there is on a clock and names the lane.
- **Families.** In `family` scope, and between two jobs of one family while `max_active_attempts_per_parent` is set,
  the family's count is what is scarce, wherever either job runs. There the older job holds the younger one as before.
  An older job that its own family's parent cap refuses cannot be placed anywhere, so it holds no job of another family.
- **Unchanged.** The kept last slot, lease FIFO, and waiters blocked on a lease that another job holds.

### 3. An auth-dead attempt closes its lane and the job moves on (C-4.5, C-23.44, C-17.3)

- An `auth-dead` attempt of a job with no lane pin is retried as a `limited` one is, with two guards:
  - it exited non-zero, because a run that exited 0 authenticated, and its verdict came from the text;
  - the job has not already been auth-dead on another lane, because two credentials failing one job points at the job.
- Without the guards, today's classifier reads the model's prose. A review that quotes the org-block text is
  classified auth-dead (#84 fixes that and is open), and the retry would disable one healthy lane after another.
- The lane is disabled in the same transaction, as before.
- A pinned job, a job out of attempts, or one stopped by either guard ends with rc 5.
- `why` names the earlier attempts. The terminal notice adds `earlier: a<n> auth-dead on <lane> (disabled; subfleet lanes enroll)`.
- Turns, revives and gate rounds have `max_attempts` 1, and resumes are pinned, so none of them moves on.

## Invariants (each with a property or example test)

1. **In-use changes nothing.** Flipping `desktop_in_use` never changes a decision. Property over drawn stores
   (`test_admission_uncapped`) and over drawn desktop cases (`test_desktop_reserve`).
2. **Last resort.** When the desktop lane is chosen, the same job with the desktop lanes removed from the view has no
   lane. Property.
3. **Reserve soundness.** A detached job is placed on the desktop lane only when no counted reading of a reserved
   window is at or above its ceiling (within the hour) and fewer than the bound are in flight. A turn is placed there
   whatever the readings say. Property, both directions.
4. **Monotone.** Raising a reserve or lowering the bound never places a job that was not placed. Property.
5. **Fresh evidence.** Detached work on the desktop lane either has a fresh account reading for each reserved window,
   or `probe_required` is true. Example tests, including the daemon's probe-then-judge path.
6. **Hold soundness.** No job is held `behind-older-job` behind a waiter that cannot take the (model, lane) it would
   take. Examples: an excluded lane, a `--no-desktop` waiter and the desktop lane, a pinned waiter, another provider's
   model, a retry's exclusion, and a full family.
7. **Fairness kept.** Where the older waiter could take that pair, the newer job is held: before the probe, inside the
   reservation, and for identical jobs with no evaluation. Examples, and the existing C-6.9 suite.
8. **Differential.** `scheduler.evaluate` equals `tests/reference_scheduler.py`. The reference writes the reserve, the
   probe flag, the scopes and the last-resort walk itself. `route_check.still_stands` equals `evaluate` on the drawn
   commits.
9. **auth-dead.** An unpinned job whose attempt was auth-dead, exited non-zero, and was its first auth-dead is never
   failed while it has attempts left. Its lane takes no attempt after that finish. A second dead lane ends the job.

## Changes since revision 1 (the design review)

- The fairness lens found three problems. `judge_lane` misses pins and providers, so the check is now pair
  membership. The pre-check ran after the probe, so the check now also runs before the probe. The parent cap and the
  family scope had no rule, so they now keep the old rule.
- The reserve lens found three problems:
  - A stale low reading is blind to Max's own use, so a fresh-evidence probe is now required.
  - The desktop lane beat promotion, so it is now the last resort across the chain.
  - The desktop mark fell back to the recorded flag, so the daemon now keeps the last hint.

  It also asked for model-scoped buckets, the hour limit on stale refusals, the room treatment of the bound, `pick`,
  `desktop_login` and the contract text.
- The auth-dead lens found three problems. The classifier reads prose, so the two guards were added. The incident was
  37 jobs, not 45. And nothing said where a retried job came from, so `why` and the notice now do.
