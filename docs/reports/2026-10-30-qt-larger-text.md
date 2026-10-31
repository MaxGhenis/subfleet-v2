# D-QT-FLAKE1: resize schedules on release/217

Base: `a093fb4b6f4d`. Reproduction: `ace8e298a013d646b596669cc5315432dd62574b`.
Product fix: `f11a788bdf1c23e10319cb5a90408c525f011505`.
Final probe correction: `39577640e` (the product geometry change is unchanged).
Started 2026-10-30 16:18:59 UTC; first test hold admitted 16:21:54 UTC.

## Causes and executed evidence

**A, pending reveal:** the failed CI capture has the reveal outstanding throughout
the clamp and every settling decision suspended. The test incorrectly requires a
new `marker_appeared` between `shrinking` and `shrunk`. None occurs. Its actual
behavioural invariant already passes. Replaying this capture against the base
order helper rejects it; the corrected geometry-based order helper accepts it,
and `assert_shrink_releases_nothing` accepts it in both versions. The early
52606 → 50368 → 52716 → 52606 document sequence precedes the scripted shrink.
The pending case's last appearance is at 0.464 s, disappearance at 0.675 s,
`shrinking` at 0.984 s and clamped `shrunk` at 1.706 s. Geometry confirms the end
while the target is still unpresented; a new lazy appearance is not necessary
for this negative test.

**A, landed reveal: a product race.** The other capture lands the target, clamps
the clip to zero, receives no new marker appearance and remains suspended through
catch-up. The target lands at 1.640 s, `shrinking` is at 1.904 s with a 7,767 pt
gap, and `shrunk` is at 2.637 s with a zero gap. The opening pin had stopped
observing the request once its target landed. A later shrink could therefore
expose the end without any event releasing
suspension. The new native `resizeSchedule` scene presents a target in a 12,000 pt
AppKit document and applies `[-11000, 2000, -2500, 1000]` without remounting a lazy
marker. At base, both clamps reach zero but deliver zero confirmations; the
landed regression fails. The pending control passes. The small product correction
retains the landed request for subsequent geometry checks, until timeline scroll input, another reveal, matching abandonment,
an own send, a conversation change or pin teardown supersedes it. ConversationView keeps
its own pending-reveal guard on these later confirmations.

The old landed CI capture still fails the fixed behavioural assertion. Accepting
either a real marker appearance or a geometry confirmation does not turn that
bad capture into a pass. Fresh native runs establish the product correction.

**B, the reply sample does not prove growth was pinned.** CI records a quiet
zero-gap frame at document height 298,584, `regrowing` at 297,507, `sent` at
293,619 and the reply at 291,017. The requested 600 pt inset growth is not a
measurement of net document growth: the document loses 3,888 pt between
`regrowing` and `sent`. The reply also triggers an ordinary follow-key jump at
76.339 s. Either mechanism can conceal a broken growth observer from the final
reply-only rule. The log lacks a document trace during that interval, so it cannot
establish the instantaneous contribution of the inset versus lazy measurement.
All eight recorded clip changes from `regrowing` through `sent` show zero gap
and successively smaller heights (297305, 296773, 296242, 295714, 295187, 294663,
294140, 293619). None demonstrates positive presented growth needing a repin.
Bounds-only sampling cannot rule out an unrecorded transient frame-only change.

The deterministic native counterexample is a quiet own-send landing followed by
`[600, -900]`: the growth mutant leaves a measured 600 pt gap, then the shrink
clamps it to zero. At base the named reply-only rule accepts it (`DID NOT RAISE`).
The fixed rule checks each resize's response before the next resize or reply.
In the isolated scene, `reply` labels the final position sample supplied to the
existing helper; the real model send and reply remain in the full quiet-view run.
The round-10 growth mutant retains its full quiet-view setup and also runs this
isolated geometry check, so an unrelated reply jump cannot supply its kill.

These schedules execute the missing-callback boundary and compensating-shrink
counterexample. They do not reproduce the exact SwiftUI estimation sequence
naturally. The four original shrink items passed at base on this host. In
particular, the original full pending-view failure was not forced naturally;
its assertion failure is reproduced from the supplied native CI capture, and
its missing-callback order is forced in the geometry probe. Prior #254 reports
and the last three fix10 changes were read; the earlier fixed-shrink, callback
ordering, quiet-zero barrier and bounded-correction lessons remain intact. The round-7 report's claim that a
landed reveal cannot miss a release relies on the marker appearing; the native
no-remount counterexample fills that gap. Round 10 already distinguished a
measured landing from later growth following; this change measures that growth
before a shrink or reply can repair its effect.

## Invariant and synchronization

While the same request and conversation remain current and its watch has not been cancelled, a shrink exposing the end releases nothing before the target is presented. After landing, it confirms the end independently of a marker remount.

The full-view replay checks the suspension decisions and caught-up position as before.

A deterministic Hypothesis property runs 12 generated schedules per invocation,
each beginning with the clamp and followed by up to eight signed resizes. It
checks pending and landed states, including growth, repeated clamps and no-op
resizes at the minimum document height. Every actual resize must deliver its
frame notification. The isolated probe drains coalesced main-queue work through
queue continuations, without a guessed duration. The full-view marker mutant
replays delivery to the real `endAppeared()` handler
through a test-only notification on the mounted scroll view, after the measured
clamp. Setup outside `raises` verifies the pending reveal, zero-gap input and
reported callback. Other shrink cases require presented geometry instead of
assuming that callback exists. Existing readiness deadlines and tolerances are
unchanged. The isolated growth response uses a 0.5 pt bound.

## Original-build validation (historical)

**201 final-source checks passed, with no failing assertion or skip.** The whole fix10 file completed in one pytest invocation (40 passed); its hold then exited cooperatively before the next file. The 80 required repetitions completed as 33 + 27 + 20, retaining completed items and deselecting them from continuation holds. The first repetition hold was interrupted before 20 minutes after a cold build consumed most of its allocation; its incomplete item is excluded. No failed assertion was rerun unchanged.

| File | Distinct items | Passing invocations |
| --- | ---: | ---: |
| `tests/frontend/test_review_r219_fix10_view.py` | 40 | 120 |
| `tests/frontend/test_review_r219_fix8_mutations.py` | 9 | 12 |
| `tests/frontend/test_review_r219_fix8_view.py` | 6 | 6 |
| `tests/fake/test_state_contract.py` | 37 | 37 |
| `tests/unit/test_test_module_names.py` | 1 | 1 |
| `tests/unit/test_invariants_index.py` | 25 | 25 |

All times below are UTC; loads are one-minute `os.getloadavg()` samples at pytest session start. The suite wrapper's `sysctl` load display is denied by the sandbox. Each hold has one serial pytest.

| Hold | Pytest start | Load | Passing checks | Pytest seconds / boundary |
| --- | --- | ---: | ---: | --- |
| `final-repeat10` | 17:24:02 | 103.21 | 33 | interrupted before 20 min |
| `final-whole-and-guards` | 17:43:44 | 84.61 | 40 | 911.09 |
| `final-repeat-pending` | 17:59:56 | 36.51 | 27 | 577.42 |
| `final-views-guards-marker` | 18:09:43 | 54.85 | 71 | 365.03 |
| `final-round7-larger` | 18:15:52 | 41.75 | 3 | 69.31 |
| `final-round10-and-landed` | 18:20:39 | 39.56 | 7 | 110.23 |
| `final-repeat-landed` | 18:22:44 | 34.43 | 20 | 572.91 |

The base shrink hold began at 16:21:54 (queued load 24.63; native landed loads 13.01/13.83) and passed all four original items. The deterministic red hold began at 16:27:04 (queued load 17.36): two intended failures and one passing pending control, 182.30 s. The native base product is unchanged in reproduction commit `ace8e298a`. The final whole-file run supplies fresh green controls and the mutation runs supply the rejecting rules. The CI pending assertion replay is recorded separately; it is not a claimed natural host failure.

| Mutant | Final executions | Rejection |
| --- | ---: | --- |
| `marker_releases_pending_reveal` | 2, default/larger | `assert_shrink_releases_nothing` |
| `confirm_by_distance_only` | 2, default/larger | `assert_shrink_releases_nothing` |
| `reveal_never_lands` | 2, default/larger | `assert_landed_shrink_releases` |
| `quiet_landing_ends_following` | 3, controlled/natural/late | reply gap in `assert_growth_and_reply_follow` |
| `growth_repins_dropped` | 2, quiet + compensating-shrink replay | measured 600 pt missed repin before the next resize |
| `quiet_window_0ms` | 1, stale glide | `assert_rests_at_the_end` |

All mutation setup/order checks execute outside the expected assertion failure. Both larger-text shrink replays and the landed mutant report actual scale 1.728, independently of the helper's default layout label. The original bad landed CI capture is still rejected by the final release assertion.

All passing final validation runs used the shared lock. One earlier direct hold followed over 30 minutes without admission; it encountered the now-corrected remount setup failure and contributes no kill. Two build-only workers warmed the six exact-source/flag cache entries with a 1,800 s build watchdog; no product or probe readiness deadline was extended. Their compiler maxima, all run starts/loads, source hashes, test phases, native red/green schedules, CI traces, and resource/cleanup audit are in [the evidence JSON](2026-10-30-qt-larger-text-evidence.json).

An initial development property assertion wrongly required a new confirmation
for a no-op resize at the minimum height. That attempt passed three items and
failed the property. The corrected property requires a fresh confirmation only
when the frame actually changes; this is included in the fix commit. It does
not weaken any product, landing or mutation assertion.

The first marker-mutant setup also failed at both layouts: it required another
appearance after changing the marker identity, but SwiftUI supplied none. These
are setup failures, not kills. In the default capture an earlier appearance had
already exercised the mutant during the clamp. The two holds were stopped after
that failure, before their other two mutants completed. `39577640e` replaces this
remaining lifecycle assumption with explicit delivery to the actual handler.
Fresh final-source validation is recorded separately from the earlier 195 passing
checks (40 whole-file, 80 repetitions, 69 views/guards and six round-10 items).

Maximum reported process RSS was 1.33 GB; the scoped live audit
recorded 80 owned identities and found none still alive.
Scratch and workspace frontend caches stored 657.4 MB at the cleanup checkpoint.

## Delivery and constraints

The requested Darwin temp directory was denied by the sandbox. Scratch is
`/private/tmp/sf-qtlarger`; both the locked sync and every run use its environment,
cache and TMPDIR. The shared git metadata refused ref creation, and the assigned
branch is checked out in the caller's worktree. Commits are therefore made on
`fix/qt-larger-text-217` in scratch-local metadata, with the original base objects
as read-only alternates, and delivered as a verified bundle. The assigned
checkout's shared HEAD remains at the base. The caller's checkout was not written. The final delivery is `qt-larger-text-217.bundle` in the assigned workspace; its ref is verified against the base repository.

The original build used no fetch, merge, push, GitHub call, live store, real
CLI/provider, sub-agent, retry-to-pass loop, skip, xfail or sleep-based fix. Every test hold contains
one serial pytest. `test_core_live.py` was not run: the lane rules prohibit it;
its person-only daemon gate is for CI. Cleanup removes scratch with the required
`/sf-` guard, plus this job's generated frontend cache in the workspace.

## Original proposed PR (historical)

Title: **Fix shrink confirmation and growth-mutant coverage on release/217**

A lazy document shrink can expose the end after a distant reveal lands without
remounting the bottom marker, leaving follow suspended. Keep confirming a landed
request from presented geometry, while preserving the outstanding-reveal guard.

Replace shrink setup's marker-callback assumption with a geometry condition,
replay delivery to the marker handler for its named mutant, and measure each scripted
growth before a compensating shrink or ordinary reply jump can hide a missed
repin. Add deterministic native counterexamples and a bounded Hypothesis resize
property. Validation details are in this report and its evidence JSON. Final validation: 201 passing checks; all six guarded mutants rejected.

Local Python was 3.14.7. The full CI matrix, Python 3.12, a CI macOS 15 image, the live app's rendered animations and unnamed mutant cases were not run. No requested local check remains unrun.

## Review r1 and fix round r1

Fix round r1b starts from `98924e35c1fd`, the byte-for-byte recovery of round r1's
code and tests after the reboot. Every result below was earned again. The
production pin, regular probe scenes and inherited tests required no further change.

1. A changed `conversationID` clears both `outstanding` and `landedRequest`.
   Assigning the same ID preserves them. Immediate and already queued inputs in
   both document orientations produce zero old-request callbacks after changing
   IDs; same-ID controls still deliver callbacks. The identity-reset mutant is
   rejected. Both identity strict marks were removed by the inherited fix.
2. One private `distanceFromEnd` helper supplies all four distance calculations
   without changing the formula. Both orientations retain 25 pt acceptance and
   26 pt rejection controls.
3. The existing scene permits up to three reveal attempts. The assertion accepts
   one through three and still requires the landed order and release behavior.
   This preserves the existing scene. A fresh native input forces two permitted
   attempts: `reveals=2` passes, while the former exact-one check rejects that
   value. The saved replay passes for 1, 2 and 3; zero reveals is rejected.
4. `releases_after` carries suspension state across the cutoff and counts only
   true-to-false transitions, including transitions recorded on movement events.
   Repeated resumed confirmations and entries without suspension data add none.
   The fresh two-reveal trace changes the old count of 41 entries
   to one real release, on `marker_appeared`. All dependent default/larger
   shrink controls and all six round-7 mutation executions pass; no mutant
   survives the corrected count. Only the release-count strict mark was removed.
5. **Listed difference retained:** quiet-landing cases lack an independent
   growth probe. Their reply-only rule accepts a measured missed 600 pt growth
   followed by a compensating shrink to zero. Its scene and strict mark remain.
   With `--runxfail`, this is an intentional assertion failure; the separate
   normal run reports a strict expected failure. These outcomes are counted
   separately below.
6. The saved #282 attempt-2 fixture exactly matches the supplied log's JSON.
   Executing it rejects the old marker-based order helper and passes the current
   geometry-based helper: a zero-distance reveal check occurs during the measured
   clamp while the target remains outstanding. Both original and corrected
   suspension/behavior assertions pass. A replay claiming landing before the
   shrink remains rejected.
7. The report and full proposed body use the qualified current-watch invariant,
   name pin teardown, add conversation change to watch cancellation, and send
   the next review to r2. No hub decision was changed.

### Suspension input and navigation

Clearing the pin's request does not call a resume callback on the old owner.
The app mounts `ConversationView(...).id(conversation_id)`, so navigation disposes
that owner's state and pin. The executed input leaves A with a pending reveal
and suspension enabled, then crosses that same identity boundary to B. The pin
is replaced, B's recorded states are resumed, catch-up jumps, and B finishes
25 pt from the end, within the existing 30 pt control. B inherits no
suspension, so no additional repair or new navigation route was added.

### Fresh red and green validation

The baseline is `26ee5d6f2`; final product/test sources are inherited
`98924e35c1fd`. Every hold contains one serial pytest and a 19-minute boundary.
The first hold began within one hour of this job's 02:34:34 UTC start.

Final selections: **170 passing repository test invocations**, plus **three
passing diagnostics**. Baseline results are separate. The quiet-landing forced
failure and normal strict expected failure are also separate.
Ten baseline source/fixture files match their `26ee5d6f2` Git blobs, and the
inherited product and test sources remain unchanged from `98924e35c1fd`.

Baseline `--runxfail`: **9 passed / 6 intended failures** in the pin file and
**3 passed** in the resize file. The cold baseline reached its 19-minute boundary
with six passing controls and two identity failures; its continuation ran only
unfinished cases. An initial diagnostic compile was stopped before producing
a native binary so its docs-path collection could explicitly load the repository's
home-isolation guard; the corrected hold passed all three diagnostics. Two queued
controllers were restarted while correcting the direct-run handoff, with their
original wait times preserved. Another queued controller was split into two
independent named-mutation sequences, and its larger-text environment was aligned
with the test's existing scale-only layout before admission. Those restarts also
retain the original wait time. These setup attempts add no passing checks.

| File | Final passing invocations | Other outcomes |
| --- | ---: | --- |
| `tests/fake/test_state_contract.py` | 37 | none |
| `tests/frontend/test_review_pr284_pin.py` | 21 | 1 failed, 1 xfailed |
| `tests/frontend/test_review_pr284_resizes.py` | 8 | none |
| `tests/frontend/test_review_r219_fix10_view.py` | 60 | none |
| `tests/frontend/test_review_r219_fix8_mutations.py` | 12 | none |
| `tests/frontend/test_review_r219_fix8_view.py` | 6 | none |
| `tests/unit/test_invariants_index.py` | 25 | none |
| `tests/unit/test_test_module_names.py` | 1 | none |

All starts below are UTC on 2026-10-31; loads use `os.getloadavg()` at pytest
start because the wrapper's `sysctl` display is denied.

| Start | Load | Run and counts per file | Seconds |
| --- | ---: | --- | ---: |
| 02:39:12 | 28.07 | `baseline-red`: test_review_pr284_pin.py: 6 passed, 2 failed; interrupted compile | 1140.00 |
| 02:44:23 | 65.54 | `diagnostics-safety-setup`: no completed cases; interrupted compile | 662.94 |
| 02:58:21 | 65.17 | `diagnostics`: diagnostics.py: 3 passed | 589.25 |
| 02:59:42 | 59.15 | `fix10-whole`: test_review_r219_fix10_view.py: 40 passed | 850.00 |
| 03:11:26 | 42.11 | `baseline-pin-remaining`: test_review_pr284_pin.py: 3 passed, 4 failed | 425.86 |
| 03:13:41 | 37.36 | `round7-default`: test_review_r219_fix8_mutations.py: 3 passed | 854.32 |
| 03:18:36 | 33.05 | `fix8-view`: test_review_r219_fix8_view.py: 6 passed | 211.42 |
| 03:22:04 | 50.53 | `baseline-resizes`: test_review_pr284_resizes.py: 3 passed | 989.70 |
| 03:22:13 | 49.44 | `repeat-larger`: test_review_r219_fix10_view.py: 20 passed | 463.50 |
| 03:39:56 | 34.25 | `fixed-pin-forced`: test_review_pr284_pin.py: 21 passed, 1 failed | 538.48 |
| 03:42:29 | 40.52 | `round10-growth`: test_review_r219_fix8_mutations.py: 2 passed | 268.55 |
| 03:47:01 | 31.17 | `round10-window`: test_review_r219_fix8_mutations.py: 1 passed | 252.07 |
| 03:48:59 | 37.70 | `fixed-resizes`: test_review_pr284_resizes.py: 8 passed | 464.53 |
| 03:58:30 | 37.90 | `round7-larger`: test_review_r219_fix8_mutations.py: 3 passed | 71.25 |
| 04:15:01 | 31.99 | `guards-quiet`: test_state_contract.py: 37 passed; test_test_module_names.py: 1 passed; test_invariants_index.py: 25 passed; test_review_pr284_pin.py: 1 xfailed | 54.69 |
| 04:16:44 | 26.14 | `round10-quiet`: test_review_r219_fix8_mutations.py: 3 passed | 253.18 |

Repeated file collection, with a narrow selector, collected exactly 20 items:
the pending and landed `[larger_text]` cases ten times each. Both collected and
passing per-node counts are checked; repeated node IDs were not used. The
whole fix10 file additionally covers both layouts, the person-scroll control
and the 12-schedule deterministic Hypothesis property.

| Mutant | Executions | Result |
| --- | ---: | --- |
| `marker_releases_pending_reveal` | 2, default/larger | rejected by pending suspension rule |
| `confirm_by_distance_only` | 2, default/larger | rejected by pending suspension rule |
| `reveal_never_lands` | 2, default/larger | rejected by landed release rule |
| `quiet_landing_ends_following` | 3 named scenes | rejected; separate listed reply-only counterexample remains |
| `growth_repins_dropped` | 2 named checks | rejected for measured growth before compensation |
| `quiet_window_0ms` | 1 | rejected by settled-position rule |

All six named mutants are rejected. The independent reset, person-scroll
cancellation and dropped-landed-watch mutants are also rejected by the review
files. Mutation setup/order checks remain outside their expected assertion
failure blocks.

After 30 minutes without admission, only the owned waiters were stopped and these selections ran directly: round7-default. The other runs used the suite wrapper. Largest reported child RSS was
1.309 GB, below 8 GB.
The final checkpoint records scratch at 799.8 MiB and this job's frontend cache at 149.9 MiB. Guarded cleanup removed scratch and this job's generated frontend cache and bytecode. Every managed tool command was awaited to completion.
Darwin scratch creation was denied, so `/private/tmp/sf-fix284r1b` held the
venv, uv cache, compiler cache and per-hold TMPDIR. Shared Git metadata denied
the index lock. Commits use `.git-local` in this worktree on
`fix/qt-larger-text-217`; each is delivered immediately in the verified
`docs/reports/qt-larger-text-217-r1b.bundle`, based on `98924e35c1fd`.
The assigned checkout's shared HEAD remains at that inherited commit.

The [evidence JSON](2026-10-30-qt-larger-text-r1b-evidence.json) retains source
hashes, collection counts, each test result, actual native inputs and reduced
native outputs. The [full body draft](qt-larger-text-r1b/pr-body-draft.md),
runner and diagnostic sources are retained in the worktree.

Not run: prohibited `tests/frontend/test_core_live.py`, a fresh CI matrix,
Python 3.12, or a CI macOS image. CI owns the live test's person-only daemon
gate. No requested local selection remains unrun. No fetch, merge, push,
GitHub call, live store, real CLI/provider, sub-agent or host-loading process
was used.

### Full proposed PR body for review r2

```markdown
**D-QT-FLAKE1: the conversation view's shrink cases and a growth mutant fail shards on open PRs on `release/217`, none of which touches a frontend file.** #282, #279 and #277 each lost a 30-minute shard to them, and #282 lost the same shard twice at one head. This PR comes from lane build `20261030-115759-qt-larger-text-b1`, on `release/217` at `a093fb4b6`.

**Causes, from the report (`docs/reports/2026-10-30-qt-larger-text.md`):**

- **Shrink under a pending reveal:** a test error. The test required a new `marker_appeared` between `shrinking` and `shrunk`, and none occurs. Its behavioural rule already passed. Replaying the failed CI capture fails the old order check and passes the geometry-based one.
- **Shrink after a distant reveal lands:** a product race. `OpeningPinView` stopped observing a request once its target landed, so a later shrink could show the end with no event to release follow suspension. A native no-remount scene shows it: at base both clamps reach zero and deliver no confirmation.
- **The growth mutant:** the reply-only rule let a compensating shrink or an ordinary reply jump hide a missed repin. A quiet own-send landing followed by `[600, -900]` leaves a measured 600 pt gap, then clamps it to zero. At base the rule accepts that run.

**The fix:**

- `OpeningPinView` keeps a landed request (`landedRequest`) and keeps checking geometry after landing. Timeline scroll input, another reveal, matching abandonment, an own send, a conversation change or pin teardown ends the watch. A conversation change clears pending and landed requests; queued geometry checks read that cleared state.
- `ConversationView` takes the new `onLandedEndVisible` callback and resumes follow only when no reveal is pending, so the pending-reveal guard stays in force.
- The shrink cases check presented geometry instead of a marker callback. The marker mutant's replay reaches the real `endAppeared()` handler through a notification that only `SUBFLEET_VIEW_TEST` builds compile.
- The growth rule checks each resize's response before the next resize or reply, with a 0.5 pt bound.

**Invariant:** While the same request and conversation remain current and its watch has not been cancelled, a shrink exposing the end releases nothing before the target is presented. After landing, it confirms the end independently of a marker remount.

**Evidence:**

- A deterministic Hypothesis property over signed resize schedules, 12 generated schedules per invocation.
- Original build: native red run with 2 intended failures and 1 passing control; 201 passing checks, including the changed cases 80 times.
- Review r1 and fix round r1: clear pending and landed requests on conversation changes, count real suspension transitions, and accept the existing scene's one-to-three reveal attempts while still requiring landing. The native `reveals=2` input and zero-reveal rejection are revalidated in round r1b.
- Fresh fix-round r1b validation: 170 passing repository test invocations and three passing diagnostics. The larger-text shrink cases ran ten times each after a 20-item collection-count check. With `--runxfail`, only the listed quiet-landing counterexample fails; normally it is a strict expected failure. All repaired cases pass.
- The saved #282 attempt-2 pending capture fails the old marker-based order helper, passes the new geometry-based helper because it measures a clamp while the target remains pending, and passes both original and corrected behavioral helpers.
- The named regression cases reject all six guarded mutants: `marker_releases_pending_reveal`, `confirm_by_distance_only`, `reveal_never_lands`, `quiet_landing_ends_following`, `growth_repins_dropped`, `quiet_window_0ms`.

**Listed difference:** quiet-landing cases still lack an independent growth probe. Measured missed growth of 600 pt followed by a compensating shrink can satisfy their reply-only rule. The strict expected-failure mark and scene stay in place for a follow-up. Repeated confirmations count as no further releases.

**Not shown or not run:** these schedules force the failing orders; they don't reproduce the exact SwiftUI estimation sequence. The original pending failure didn't recur naturally on the lane host, so its evidence is the CI capture and the forced order. `tests/frontend/test_core_live.py`, the CI matrix and Python 3.12 didn't run on the lane; CI runs them.

**Next:** review r2. Its APPROVE and completed CI are the merge gates.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```
