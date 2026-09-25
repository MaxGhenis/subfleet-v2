"""C-6.9: properties of the hold rule that hold for every queue, not only the examples.

`scheduler.tier_hold` decides, for one later job, which older waiter of its tier
holds it back and which lanes it may not take on this pass. These properties
state the rule's invariants and check them over generated queues:

- two jobs pinned to different lanes never compete, and a waiter the later job
  does not compete with changes nothing;
- with no lane pins anywhere, FIFO is exactly what it was before lane pins
  (differential against the pre-2026-09-22 rule);
- the oldest competing waiter pinned to a lane is the one that keeps it;
- a job is held only behind a waiter that could use any lane, or when every lane
  it could use is kept; a pin that names several lanes is never held;
- `dominant_rejection` and `kept_only` agree on which lanes are kept.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from subfleet import scheduler

MODELS = ("fable", "opus", "astra")
LANES = ("claude-a", "claude-b", "claude-c", "codex-1")

model_sets = st.none() | st.frozensets(st.sampled_from(MODELS), min_size=1)
# A lane pin resolves to one lane id, or to none (the empty set); None is no pin. Wider sets
# are not produced by `demand_lanes`, but the rule is stated for any L, so they are allowed.
lane_sets = st.none() | st.frozensets(st.sampled_from(LANES), max_size=3)


@st.composite
def queues(draw):
    """(later models, later lanes, pinned, waiters oldest first)."""
    n = draw(st.integers(0, 6))
    waiters = [(f"w{i}", draw(model_sets), draw(lane_sets)) for i in range(n)]
    models, lanes = draw(model_sets), draw(lane_sets)
    # A job with lanes None either has no pin or has one that names several lanes.
    pinned = lanes is not None or draw(st.booleans())
    return models, lanes, pinned, waiters


def competing(models, lanes, waiters):
    return [w for w in waiters if scheduler.competes(models, w[1], lanes, w[2])]


@settings(max_examples=500)
@given(model_sets, model_sets, lane_sets, lane_sets)
def test_c6_9_jobs_pinned_to_different_lanes_never_compete(models, other, lanes, other_lanes):
    if lanes is not None and other_lanes is not None and not lanes & other_lanes:
        assert not scheduler.competes(models, other, lanes, other_lanes)
    # symmetric, whatever the pins
    assert scheduler.competes(models, other, lanes, other_lanes) == scheduler.competes(other, models, other_lanes, lanes)


@settings(max_examples=500)
@given(queues(), st.data())
def test_c6_9_a_waiter_the_job_does_not_compete_with_changes_nothing(queue, data):
    models, lanes, pinned, waiters = queue
    extra = ("x", data.draw(model_sets), data.draw(lane_sets))
    if scheduler.competes(models, extra[1], lanes, extra[2]):
        return
    at = data.draw(st.integers(0, len(waiters)))
    assert scheduler.tier_hold(models, lanes, waiters[:at] + [extra] + waiters[at:], pinned=pinned) == \
        scheduler.tier_hold(models, lanes, waiters, pinned=pinned)


def old_rule(models, lanes, waiters):
    """Admission before 2026-09-22: the first competing waiter holds the job back; nothing is kept."""
    return next((older for older, theirs, their_lanes in waiters
                 if scheduler.competes(models, theirs, lanes, their_lanes)), None), {}


@settings(max_examples=500)
@given(model_sets, st.lists(model_sets, max_size=6))
def test_c6_9_with_no_lane_pins_fifo_is_unchanged(models, theirs):
    waiters = [(f"w{i}", m, None) for i, m in enumerate(theirs)]
    assert scheduler.tier_hold(models, None, waiters) == old_rule(models, None, waiters)


@settings(max_examples=500)
@given(queues())
def test_c6_9_the_oldest_competing_waiter_pinned_to_a_lane_keeps_it(queue):
    models, lanes, pinned, waiters = queue
    behind, kept = scheduler.tier_hold(models, lanes, waiters, pinned=pinned)
    if pinned and lanes is None:
        return                      # names several lanes: nothing is kept from it (the next property)
    ids = [w[0] for w in waiters]
    upto = ids.index(behind) + 1 if behind else len(waiters)
    for lane, older in kept.items():
        first = next(w[0] for w in competing(models, lanes, waiters[:upto])
                     if w[2] is not None and lane in w[2])
        assert older == first
    # and every lane such a waiter is pinned to, up to the one that holds the job, is kept
    expected = {lane for w in competing(models, lanes, waiters[:upto]) if w[2] is not None for lane in w[2]}
    assert set(kept) == expected


@settings(max_examples=500)
@given(queues())
def test_c6_9_a_job_is_held_only_behind_a_free_choice_or_when_every_lane_it_could_use_is_kept(queue):
    models, lanes, pinned, waiters = queue
    behind, kept = scheduler.tier_hold(models, lanes, waiters, pinned=pinned)
    rivals = competing(models, lanes, waiters)
    if pinned and lanes is None:                                        # names several lanes: refused (C-6.12)
        assert (behind, kept) == (None, {})
        return
    if lanes == frozenset():                                            # names no lane: competes with nothing
        assert (behind, kept) == (None, {})
        return
    if behind is None:
        assert all(w[2] is not None for w in rivals)
        assert lanes is None or not lanes <= kept.keys()
        return
    ids = [w[0] for w in rivals]
    assert behind in ids
    older = rivals[ids.index(behind)]
    assert older[2] is None or (lanes is not None and lanes <= kept.keys())
    # the first such waiter: nothing earlier would already have held it
    for i, earlier in enumerate(rivals[:ids.index(behind)]):
        so_far = {lane for w in rivals[:i + 1] if w[2] is not None for lane in w[2]}
        assert earlier[2] is not None and not (lanes is not None and lanes <= so_far)


@settings(max_examples=300)
@given(queues(), st.data())
def test_c6_9_a_younger_waiter_changes_nothing_once_the_job_is_held_and_adds_only_new_lanes_otherwise(queue, data):
    models, lanes, pinned, waiters = queue
    extra = ("y", data.draw(model_sets), data.draw(lane_sets))
    before = scheduler.tier_hold(models, lanes, waiters, pinned=pinned)
    after = scheduler.tier_hold(models, lanes, waiters + [extra], pinned=pinned)
    if before[0] is not None:
        assert after == before
    else:
        assert {lane: after[1][lane] for lane in before[1]} == before[1]


@settings(max_examples=200)
@given(queues())
def test_c6_9_tier_hold_reads_the_waiters_once(queue):
    models, lanes, pinned, waiters = queue
    assert scheduler.tier_hold(models, lanes, iter(waiters), pinned=pinned) == \
        scheduler.tier_hold(models, lanes, list(waiters), pinned=pinned)


# --- C-6.11: the label and the kept lanes agree -----------------------------------------------------

REASONS = ("no-slot", "excluded", "closed:account:2026-09-26T00:00:00Z", "reserve:fable:unmeasured",
           "kept:w1", "kept:w2")


@st.composite
def decisions(draw):
    rows = []
    for lane in draw(st.lists(st.sampled_from(LANES), unique=True, max_size=4)):
        own = draw(st.lists(st.sampled_from(REASONS[:4]), unique=True, max_size=2))
        kept = draw(st.sampled_from((None, "kept:w1", "kept:w2")))
        reasons = own + ([kept] if kept else [])                        # kept is appended last, as evaluate does
        if reasons:
            rows.append({"lane_id": lane, "reason": reasons[0], "reasons": reasons})
    blocks = draw(st.sampled_from(([], ["fleet"], ["parent:p1"])))
    return {"evaluations": [{"model": "fable", "rejections": rows, "capacity_blocks": blocks}]}


@settings(max_examples=500)
@given(decisions())
def test_c6_11_a_kept_label_means_a_lane_kept_and_nothing_with_room(decision):
    label = scheduler.dominant_rejection(decision)
    kept = scheduler.kept_only(decision)
    rows = decision["evaluations"][0]["rejections"]
    room = any(set(row["reasons"]) == {"no-slot"} for row in rows)
    if label.startswith("kept:"):
        assert not room and kept and label == "kept:" + next(iter(kept.values()))
    else:
        assert room or not kept
    for row in rows:                                                    # a lane that refuses the job anyway is not kept
        standing = [reason for reason in row["reasons"] if reason != "no-slot"]
        assert (row["lane_id"] in kept) == (len(standing) == 1 and standing[0].startswith("kept:"))
