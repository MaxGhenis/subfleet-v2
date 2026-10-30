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
retains the landed request for subsequent geometry checks, until input, another
reveal, abandonment, an own send or removal supersedes it. ConversationView keeps
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

For every resize schedule, while a reveal's target remains unpresented and no
person input supersedes it, showing the end by shrinking the document must not
release its suspension. After that target has landed, a shrink exposing the end
must confirm it independently of whether a lazy marker remounts. Once released,
catch-up follows the end. The full-view replay checks the suspension decisions
and caught-up position as before.

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

## Validation

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

No fetch, merge, push, GitHub call, live store, real CLI/provider, sub-agent,
retry-to-pass loop, skip, xfail or sleep-based fix was used. Every test hold contains
one serial pytest. `test_core_live.py` was not run: the lane rules prohibit it;
its person-only daemon gate is for CI. Cleanup removes scratch with the required
`/sf-` guard, plus this job's generated frontend cache in the workspace.

## Proposed PR

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
