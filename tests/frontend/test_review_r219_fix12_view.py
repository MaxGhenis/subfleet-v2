"""Round 8 (review r6 of round 7): three ways the view stayed behind the end.

- A reveal whose target leaves the model, or was never in it, is abandoned at once,
  and the view reconciles with the end (P2-1).
- The person's own send ends a reveal and releases suspension, catch-up included (P2-2).
- The pin's mark check runs whenever the mark's place in the document changes, and
  when a reused mark takes the target's id (P2-3).
"""
import json

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.test_review_r219_fix8_view import END, fix8_view_binary
from tests.frontend.test_review_r219_fix10_view import DISTANT

pytestmark = needs_swift
# A followed reply: the end marker's 24 pt column padding plus layout rounding.
GAP = 25


def fix12_run(binary, root, mode):
    result = run_probe(binary, root, f"fix12-{mode}", timeout=240)
    write_json(root / "result.json", result)
    diagnostic = json.dumps(result, sort_keys=True)
    print(f"fix12 {mode}: " + diagnostic)
    assert result["visible_windows"] == 0, diagnostic
    return result, {step["step"]: step for step in result.get("steps", [])}, diagnostic


def follow_state(result, at):
    """The view's follow state as last recorded at or before `at`."""
    entries = [e for e in result["trace"] if "suspended" in e and e["at"] <= at]
    return entries[-1] if entries else {"suspended": False, "at_bottom": True, "event": "none"}


def releases_between(result, start, end):
    """Each trace entry in [start, end] that turned suspension off."""
    released, suspended = [], follow_state(result, start)["suspended"]
    for entry in result["trace"]:
        if "suspended" not in entry or not start <= entry["at"] <= end:
            continue
        if suspended and not entry["suspended"]:
            released.append(entry)
        suspended = entry["suspended"]
    return released


def abandonments(result, reason):
    return [e for e in result["trace"] if e["event"] == "reveal_abandoned" and e.get("reason") == reason]


# P2-1: a reveal whose target goes away is abandoned, and the view reconciles.

def assert_removed_target_order(result, steps):
    """The review's input as presented: a pending distant reveal, then a reset that takes its
    target out of the model while catch-up is held, with the clip clamped to the shrunk end."""
    diagnostic = json.dumps(result, sort_keys=True)
    assert steps["opened"]["position"]["from_end"] <= END, diagnostic
    assert steps["pending"]["target_in_model"] and steps["pending"]["position"]["from_end"] > DISTANT, diagnostic
    assert follow_state(result, steps["pending"]["at"])["suspended"], diagnostic
    assert steps["reset"]["resets"] >= 1 and not steps["reset"]["target_in_model"], diagnostic
    assert steps["reset"]["readiness"]["catchup_pending"], diagnostic
    assert steps["reset"]["position"]["from_end"] <= END, diagnostic
    assert steps["caught_up"]["readiness"]["caught_up"] and steps["caught_up"]["target_in_model"], diagnostic


def assert_removed_target_releases(result, steps):
    """Abandoned when the row left the model; the end then in view released suspension, and
    the next reply is followed."""
    diagnostic = json.dumps(result, sort_keys=True)
    abandoned = abandonments(result, "target_removed")
    assert abandoned and abandoned[0]["at"] <= steps["reset"]["at"], diagnostic
    assert not follow_state(result, steps["reset"]["at"])["suspended"], diagnostic
    assert steps["reply"]["position"]["from_end"] <= GAP, diagnostic


def test_reset_removing_a_pending_reveals_target_abandons_it(fix8_view_binary, tmp_path):
    """Review r6 P2-1: a pending reveal to `text:m000040:b`, then an event-log reset that removed
    that row. The clip reached the end and the marker appeared, but suspension stayed on, and the
    next reply left the view 1,050 pt behind."""
    result, steps, _ = fix12_run(fix8_view_binary, tmp_path, "removed")
    assert_removed_target_order(result, steps)
    assert_removed_target_releases(result, steps)


def test_reveal_of_a_target_missing_from_the_model_is_abandoned(fix8_view_binary, tmp_path):
    """A request whose target the model does not hold is abandoned at once: with the end in
    view, suspension is released, and the next reply is followed."""
    result, steps, diagnostic = fix12_run(fix8_view_binary, tmp_path, "missing")
    assert steps["opened"]["position"]["from_end"] <= END, diagnostic
    assert not steps["reveal"]["target_in_model"], diagnostic
    abandoned = abandonments(result, "target_missing")
    assert abandoned and abandoned[0]["at"] <= steps["reveal"]["at"], diagnostic
    assert not follow_state(result, steps["requested"]["at"])["suspended"], diagnostic
    assert steps["reply"]["position"]["from_end"] <= GAP, diagnostic


def assert_missing_away_order(result, steps):
    """The request came with the end out of view, and the end then came into view with no
    person input."""
    diagnostic = json.dumps(result, sort_keys=True)
    assert steps["opened"]["position"]["from_end"] <= END, diagnostic
    requests = [e for e in result["trace"] if e["event"] == "reveal"]
    assert len(requests) == 1 and not requests[0]["at_bottom"], diagnostic
    assert not steps["reveal"]["target_in_model"], diagnostic
    assert steps["requested"]["position"]["from_end"] > DISTANT, diagnostic
    assert steps["end_shown"]["position"]["from_end"] <= END, diagnostic
    assert not any(e["event"] in ("person_scrolled", "move") for e in result["trace"]
                   if steps["reveal"]["at"] <= e["at"] <= steps["end_shown"]["at"]), diagnostic


def assert_missing_away_releases(result, steps):
    """Abandoned at the request, which released nothing with the end away; the marker's
    appearance then released suspension, and the next reply is followed."""
    diagnostic = json.dumps(result, sort_keys=True)
    assert abandonments(result, "target_missing"), diagnostic
    assert follow_state(result, steps["requested"]["at"])["suspended"], diagnostic
    released = releases_between(result, steps["showing"]["at"], steps["end_shown"]["at"])
    assert released and released[0]["event"] == "marker_appeared", diagnostic
    assert steps["reply"]["position"]["from_end"] <= GAP, diagnostic


def test_missing_target_away_from_the_end_releases_when_the_end_appears(fix8_view_binary, tmp_path):
    """With the end out of view the abandoned request releases nothing, and from then on the
    normal rules apply: the end coming into view without the person releases suspension."""
    result, steps, _ = fix12_run(fix8_view_binary, tmp_path, "missing-away")
    assert_missing_away_order(result, steps)
    assert_missing_away_releases(result, steps)


def test_removed_target_away_from_the_end_holds_until_the_end_is_shown(fix8_view_binary, tmp_path):
    """The control: the same reset with the end out of view abandons the reveal but releases
    nothing; through catch-up and a reply the view stays, until the person scrolls to the end
    and the marker appears. Then the next reply is followed."""
    result, steps, diagnostic = fix12_run(fix8_view_binary, tmp_path, "removed-away")
    assert steps["pending"]["target_in_model"] and steps["pending"]["position"]["from_end"] > DISTANT, diagnostic
    assert not steps["reset"]["target_in_model"] and steps["reset"]["position"]["from_end"] > DISTANT, diagnostic
    assert not releases_between(result, steps["reveal"]["at"], steps["person_scrolling"]["at"]), diagnostic
    held = follow_state(result, steps["reply_held"]["at"])
    assert held["suspended"] and steps["reply_held"]["position"]["from_end"] > DISTANT, diagnostic
    assert steps["person_at_end"]["position"]["from_end"] <= END, diagnostic
    assert releases_between(result, steps["person_scrolling"]["at"], steps["person_at_end"]["at"]), diagnostic
    assert steps["reply"]["position"]["from_end"] <= GAP, diagnostic


# P2-2: the person's own send ends a reveal, even during catch-up.

def assert_own_send_order(result, steps, *, catchup):
    diagnostic = json.dumps(result, sort_keys=True)
    assert steps["opened"]["position"]["from_end"] <= END, diagnostic
    assert steps["pending"]["position"]["from_end"] > DISTANT, diagnostic
    assert follow_state(result, steps["pending"]["at"])["suspended"], diagnostic
    assert steps["sent"]["own_send_last"], diagnostic
    readiness = steps["sent"]["readiness"]
    if catchup:
        assert result["held_before_reveal"], diagnostic
        assert readiness["catchup_pending"] and not readiness["caught_up"], diagnostic
        assert steps["caught_up"]["readiness"]["caught_up"], diagnostic
    else:
        assert readiness["caught_up"], diagnostic


def assert_own_send_releases(result, steps, *, catchup):
    """The send released suspension, every later catch-up move went to the end, and the reply
    is followed."""
    diagnostic = json.dumps(result, sort_keys=True)
    released = releases_between(result, steps["pending"]["at"], steps["sent"]["at"])
    assert released, diagnostic
    assert not follow_state(result, steps["sent"]["at"])["suspended"], diagnostic
    if catchup:
        settling = [e for e in result["trace"] if e["at"] >= released[0]["at"] and e["event"] == "move"
                    and e["settling"]]
        assert settling and all(not e["suspended"] and e["move"] == "jump" for e in settling), diagnostic
    assert steps["reply"]["position"]["from_end"] <= GAP, diagnostic


def test_own_send_during_catchup_ends_a_pending_reveal(fix8_view_binary, tmp_path):
    """Review r6 P2-2: `UIModel.send` during a pending reveal with catch-up held chose `stay`:
    2,232 pt behind, and 18,529 pt after catch-up and a reply."""
    result, steps, _ = fix12_run(fix8_view_binary, tmp_path, "send-catchup")
    assert_own_send_order(result, steps, catchup=True)
    assert_own_send_releases(result, steps, catchup=True)


def test_own_send_during_a_pending_reveal_releases_while_following(fix8_view_binary, tmp_path):
    """The ordinary-following control: the same send once caught up glides and releases."""
    result, steps, _ = fix12_run(fix8_view_binary, tmp_path, "send-ordinary")
    assert_own_send_order(result, steps, catchup=False)
    assert_own_send_releases(result, steps, catchup=False)


# P2-3: the mark is checked whenever its place in the document changes.

@pytest.fixture(scope="module")
def mark_probes(fix8_view_binary, tmp_path_factory):
    root = tmp_path_factory.mktemp("fix12-mark")
    result = run_probe(fix8_view_binary, root, "fix12-mark", timeout=240)
    write_json(root / "result.json", result)
    print("mark probes: " + json.dumps(result, sort_keys=True))
    assert result["visible_windows"] == 0
    return result


def assert_mark_scene_order(scene):
    """The reveal was outstanding, and only the mark's place or identity changed: the clip, the
    document and the mark's own frame stayed."""
    diagnostic = json.dumps(scene, sort_keys=True)
    before, after = scene["before"], scene["after"]
    assert before["landed"] == 0 and before["end_visible"] == 0, diagnostic
    assert any(e["event"] == "reveal_check" and not e["presented"] for e in scene["trace"]
               if e["at"] < scene["moved_at"]), diagnostic
    for key in ("offset", "content", "viewport"):
        assert after[key] == before[key], diagnostic
    assert after["mark_frame"] == before["mark_frame"], diagnostic
    if scene["kind"] == "identity":
        assert after["mark_in_document"] == before["mark_in_document"], diagnostic
        assert (before["mark_id"], after["mark_id"]) == ("another-row", "target"), diagnostic
    else:
        assert after["mark_in_document"]["y"] != before["mark_in_document"]["y"], diagnostic
        assert after["mark_id"] == "target", diagnostic


def assert_mark_probes_order(result):
    for kind in ("swiftui", "parent", "identity"):
        assert_mark_scene_order(result[kind])


def assert_mark_probes_run_the_check(result):
    """Every scene's change ran the check and released or kept suspension correctly."""
    for kind, released in (("swiftui", True), ("parent", False), ("identity", True)):
        assert_mark_check_ran(result[kind], released=released)


def assert_mark_check_ran(scene, *, released):
    """The mark's change ran the check, which found the row presented: the reveal landed, and
    released suspension exactly when the end was in the clip beside it."""
    diagnostic = json.dumps(scene, sort_keys=True)
    checks = [e for e in scene["trace"] if e["event"] == "reveal_check" and e["at"] >= scene["moved_at"]]
    assert checks and checks[0]["source"] == "mark" and checks[0]["presented"], diagnostic
    assert len(scene["landed_at"]) == 1 and scene["landed_at"][0] >= scene["moved_at"], diagnostic
    assert len(scene["end_visible_at"]) == int(released), diagnostic
    assert (scene["after"]["from_end"] <= 1) == released, diagnostic


def test_swiftui_layout_moving_the_marks_host_runs_the_check(mark_probes):
    """Review r6 P2-3: SwiftUI moved the row's host from y=300 to y=1,700 under a clip at the end;
    the row and the end were visible together, and neither callback ran."""
    scene = mark_probes["swiftui"]
    assert_mark_scene_order(scene)
    assert scene["before"]["mark_in_document"]["y"] == 300 and scene["after"]["mark_in_document"]["y"] == 1700
    assert_mark_check_ran(scene, released=True)


def test_appkit_parent_moving_the_mark_runs_the_check(mark_probes):
    """An ancestor moves the mark into the clip with the end out of view: landed, still held."""
    scene = mark_probes["parent"]
    assert_mark_scene_order(scene)
    assert_mark_check_ran(scene, released=False)


def test_reused_mark_taking_the_targets_id_runs_the_check(mark_probes):
    """A reused mark already in the clip at the end takes the target's id: landed, released."""
    scene = mark_probes["identity"]
    assert_mark_scene_order(scene)
    assert_mark_check_ran(scene, released=True)
