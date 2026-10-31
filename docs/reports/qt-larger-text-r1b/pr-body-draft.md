**D-QT-FLAKE1: the conversation view's shrink cases and a growth mutant fail shards on open PRs on `release/217`, none of which touches a frontend file.** #282, #279 and #277 each lost a 30-minute shard to them, and #282 lost the same shard twice at one head. This PR comes from lane build `20261030-115759-qt-larger-text-b1`, on `release/217` at `a093fb4b6`.

**Causes, from the report (`docs/reports/2026-10-30-qt-larger-text.md`):**
- **Shrink under a pending reveal:** a test error. The test required a new `marker_appeared` between `shrinking` and `shrunk`, and none occurs. Its behavioural rule already passed. Replaying the failed CI capture fails the old order check and passes the geometry-based one.
- **Shrink after a distant reveal lands:** a product race. `OpeningPinView` stopped observing a request once its target landed, so a later shrink could show the end with no event to release follow suspension. A native no-remount scene shows it: at base both clamps reach zero and deliver no confirmation.
- **The growth mutant:** the reply-only rule let a compensating shrink or an ordinary reply jump hide a missed repin. A quiet own-send landing followed by `[600, -900]` leaves a measured 600 pt gap, then clamps it to zero. At base the rule accepts that run.

**The fix:**
- `OpeningPinView` keeps a landed request (`landedRequest`) and keeps checking geometry after landing. Timeline scroll input, another reveal, matching abandonment, an own send, a conversation change or pin teardown ends the watch. A conversation change clears pending and landed requests, including checks already queued.
- `ConversationView` takes the new `onLandedEndVisible` callback and resumes follow only when no reveal is pending, so the pending-reveal guard stays in force.
- The shrink cases check presented geometry instead of a marker callback. The marker mutant's replay reaches the real `endAppeared()` handler through a notification that only `SUBFLEET_VIEW_TEST` builds compile.
- The growth rule checks each resize's response before the next resize or reply, with a 0.5 pt bound.

**Invariant:** While the same request and conversation remain current and its watch has not been cancelled, a shrink exposing the end releases nothing before the target is presented. After landing, it confirms the end independently of a marker remount.

**Evidence:**
- A deterministic Hypothesis property over signed resize schedules, 12 generated schedules per invocation.
- Original build: native red run with 2 intended failures and 1 passing control; 201 passing checks, including the changed cases 80 times.
- Review r1 and fix round r1: clear pending and landed requests on conversation changes, count real suspension transitions, and accept the existing scene's one-to-three reveal attempts while still requiring landing. The native `reveals=2` input and zero-reveal rejection are revalidated in round r1b.
- Fix-round validation is being rerun at inherited head `98924e35c1fd`; final counts will be recorded here. The saved #282 attempt-2 pending capture is checked through the old marker-based and new geometry-based order helpers and both behavioral versions.
- The named regression cases reject all six guarded mutants: `marker_releases_pending_reveal`, `confirm_by_distance_only`, `reveal_never_lands`, `quiet_landing_ends_following`, `growth_repins_dropped`, `quiet_window_0ms`.

**Listed difference:** quiet-landing cases still lack an independent growth probe. Measured missed growth of 600 pt followed by a compensating shrink can satisfy their reply-only rule. The strict expected-failure mark and scene stay in place for a follow-up. Repeated confirmations count as no further releases.

**Not shown or not run:** these schedules force the failing orders; they don't reproduce the exact SwiftUI estimation sequence. The original pending failure didn't recur naturally on the lane host, so its evidence is the CI capture and the forced order. `tests/frontend/test_core_live.py`, the CI matrix and Python 3.12 didn't run on the lane; CI runs them.

**Next:** review r2. Its APPROVE and completed CI are the merge gates.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
