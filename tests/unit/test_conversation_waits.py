"""C-24.4, C-29.11 (I3): a waiting message says why, and says capacity only when it is.

`subfleet/conversations/waits.py` turns admission's hold (C-6.11) into the message's
`state_reason`, `<kind>: <detail>`. The app words only the `capacity` kind as
waiting for capacity (tests/frontend/test_core_timeline.py), so the property here
is that the kind is `capacity` exactly when some lane would take the turn once
room or usage frees, or a turn cap is what holds it.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from subfleet.conversations import waits

ROOM = ["no-slot", "below-floor", "reserve:fable:reserved"]
STANDING = ["disabled", "identity-mismatch", "config-dir", "owner-v1", "excluded", "desktop",
            "reserve:fable:probe-required", "reserve:fable:unmeasured"]
CLOSED = ["closed:account:2026-09-29T14:05:00Z", "closed:gpt-6-astra:2026-09-29T13:00:00Z"]


def decision(rows, provider="claude", blocks=()):
    return {"evaluations": [{"provider": provider, "capacity_blocks": list(blocks),
                             "rejections": [{"lane_id": lane, "reason": reasons[0], "reasons": reasons}
                                            for lane, reasons in rows]}]}


def reason_of(rows, label="no-slot", **kw):
    return waits.hold_reason({"reason": label, "lanes": waits.lane_summary(decision(rows, **kw))},
                             describe=str, who=str)


def test_every_lane_busy_is_capacity_and_counts_them():
    text = reason_of([("c1", ["no-slot"]), ("c2", ["no-slot"]), ("c3", ["below-floor"])])
    assert text == "capacity: no Claude lane has room for it yet (2 busy; 1 at its usage floor)"


def test_closed_accounts_name_the_first_reset_and_are_never_capacity():
    text = reason_of([("c1", ["closed:account:2026-09-29T14:05:00Z", "no-slot"]),
                      ("c2", ["closed:account:2026-09-29T13:00:00Z"]), ("c3", ["disabled"])], "closed:account")
    assert text == ("closed: every Claude lane that could take it is closed until 2026-09-29 13:00 UTC at the "
                    "earliest (2 closed; 1 disabled)")


def test_a_closed_lane_beside_a_busy_one_waits_for_room_and_says_when_the_other_reopens():
    text = reason_of([("c1", ["no-slot"]), ("c2", ["closed:account:2026-09-29T13:00:00Z"])])
    assert text == "capacity: no Claude lane has room for it yet (1 busy; 1 closed until 2026-09-29 13:00 UTC)"


def test_standing_refusals_are_no_lane_and_stale_usage_is_its_own_kind():
    assert reason_of([("x1", ["disabled"]), ("x2", ["identity-mismatch"])], "disabled", provider="codex") == (
        "no-lane: no Codex lane can take it (1 disabled; 1 signed in to another account)")
    assert reason_of([("c1", ["reserve:fable:unmeasured"])], "reserve:fable:unmeasured").startswith(
        "usage-unknown: no Claude lane can take it until its usage is read again")


def test_a_turn_cap_is_capacity_and_no_enrolled_lane_is_not():
    assert reason_of([("c1", ["no-slot"])], blocks=["fleet"]).startswith(
        "capacity: the conversations' turn pool is at its policy cap")
    assert waits.hold_reason({"reason": "fleet-full", "max_active_attempts": 2}, describe=str, who=str).startswith(
        "capacity: the conversations' turn pool is full (2 at once")
    assert waits.hold_reason({"reason": "no-lanes"}, describe=str, who=str) == (
        "no-lane: no enrolled lane can take this conversation's model")


def test_a_lease_names_its_holder_and_a_settled_message_needs_no_reason():
    described = []

    def describe(key):
        described.append(key)
        return f"{key} is taken"
    hold = {"reason": "lease-held", "leases": ["worktree:/repo"], "queued": ["conversation:c"],
            "queued_behind": ["20260929-120000-turn"]}
    assert waits.hold_reason(hold, describe=describe, who=lambda job: f"turn {job}") == (
        "lease: worktree:/repo is taken; an older turn waiting for the same thing goes first: turn 20260929-120000-turn")
    assert described == ["worktree:/repo"]
    assert waits.hold_reason({"reason": "message-settled"}, describe=describe, who=str) is None


HOLDS = st.one_of(
    st.builds(lambda reason: {"reason": reason},
              st.sampled_from(["attempt-live", "route-moved", "probe-pending", "approval", "uncertain", "waiting",
                               "not-evaluated", "workspace", "route", "conversation-blocked"])),
    st.builds(lambda leases: {"reason": "lease-held", "leases": leases},
              st.lists(st.sampled_from(["worktree:/r", "conversation:c", "native:claude:s"]), max_size=3)),
    st.builds(lambda n: {"reason": "fleet-full", "max_active_attempts": n}, st.integers(1, 9)),
    st.builds(lambda job: {"reason": "slot-kept", "kept_for": job}, st.just("20260929-120000-a")),
    st.builds(lambda job: {"reason": "behind-older-job", "behind": job}, st.just("20260929-120000-a")),
)
LANE_ROWS = st.lists(st.tuples(st.sampled_from(["c1", "c2", "c3", "c4"]),
                               st.lists(st.sampled_from(ROOM + STANDING + CLOSED), min_size=1, max_size=3)),
                     max_size=5, unique_by=lambda row: row[0])


@settings(max_examples=500, deadline=None)
@given(hold=HOLDS)
def test_i3_every_hold_but_a_settled_message_has_a_reason_with_a_kind(hold):
    text = waits.hold_reason(hold, describe=lambda key: f"{key} is taken", who=str)
    kind, sep, detail = text.partition(": ")
    assert sep and detail and kind in ("capacity", "closed", "usage-unknown", "no-lane", "lease", "blocked",
                                       "workspace", "route", "admission")
    assert (kind == "capacity") == (hold["reason"] in ("fleet-full", "slot-kept", "behind-older-job"))


@settings(max_examples=500, deadline=None)
@given(rows=LANE_ROWS, provider=st.sampled_from(["claude", "codex"]), pool_cap=st.booleans())
def test_i3_no_lane_is_capacity_exactly_when_some_lane_waits_only_for_room(rows, provider, pool_cap):
    """The hold is built as admission builds it: its label is `scheduler.dominant_rejection`'s
    and a turn cap marks every lane `no-slot`. The kind is `capacity` exactly when some
    lane's standing reason (the first that is not `no-slot`, else `no-slot`) is room or
    usage; a closed-only fleet names its first reset."""
    from subfleet import scheduler
    if pool_cap:
        rows = [(lane, [*reasons, "no-slot"]) for lane, reasons in rows]
    made = decision(rows, provider, ["fleet"] if pool_cap and rows else ())
    label = scheduler.dominant_rejection(made)
    hold = {"reason": label, "lanes": waits.lane_summary(made),
            **({"max_active_attempts": 3} if label == "fleet-full" else {})}
    text = waits.hold_reason(hold, describe=str, who=str)
    kind = text.partition(": ")[0]
    standing = [waits.lane_label(reasons) for _, reasons in rows]
    assert (kind == "capacity") == any(each in ROOM for each in standing), (rows, label, text)
    if standing and all(each.startswith("closed:") for each in standing):
        first = min(each.split(":", 2)[2] for each in standing)
        assert kind == "closed" and waits.when(first) in text, text
    if not rows:
        assert kind == "no-lane"
