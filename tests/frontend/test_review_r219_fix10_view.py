"""Distant search reveals with both gated output and overlapping streams."""
import json
import os

import pytest
from hypothesis import Phase, given, settings, strategies as st

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.test_review_r219_fix8_view import END, fix8_view_binary

pytestmark = needs_swift
DISTANT = 600
# A distant reveal's destination is its row, not the end. After the final scroll,
# lazy measurement of rows above it moves the offset without moving the view: an
# unmutated loaded stale-marker run moved 1,942 pt in offset while coming only
# 360 pt closer to the end (round 6). The interval is therefore measured by
# distance from the end. Measurement below the row also brings the end closer:
# 20 unmutated loaded background runs came up to 559.5 pt closer, too near the
# 600 pt margin, so the bound is 1,200 pt (a later loaded run came 724.5 pt
# closer). Every presented overlap loss in the round-6 mutant runs reached
# within 130 pt of the end, as did review r5's survivor (177 pt).
REVEAL_HOLD = 1200
FAR = {"far_quiet": 0, "far_text_50ms": 50, "far_text_120ms": 21,
       "far_rows_150ms": 17, "far_rows_400ms": 7}


@pytest.fixture(scope="module")
def distant_reveals(fix8_view_binary, tmp_path_factory):
    root = tmp_path_factory.mktemp("fix10-distant")
    result = run_probe(fix8_view_binary, root, "fix10", timeout=600)
    write_json(root / "result.json", result)
    print("distant reveal positions: " + json.dumps(result, sort_keys=True))
    assert result["visible_windows"] == 0
    assert result["mounted_windows"] == 7 and result["animation_calls"] > 0
    return result


@pytest.mark.parametrize("name", FAR)
def test_distant_search_reveal_lands_and_keeps_position(distant_reveals, name):
    run = distant_reveals[name]
    diagnostic = json.dumps(run, sort_keys=True)
    assert run["found"] and run["reveal_consumed"] and run["caught_up"], diagnostic
    assert len(run["notices_at"]) == 2 and run["reveal_starts"] == 1, diagnostic
    assert run.get("output_released"), diagnostic
    before = run["before_output"]
    assert before["consumed"] and before["notices"] == 2, diagnostic
    assert before["readiness"]["caught_up"] and not before["readiness"]["reveal_pending"], diagnostic
    assert before["stable_samples"] >= 3 and before["position"]["from_end"] > 600, diagnostic
    assert run["folded_seq"] == run["target_seq"], diagnostic
    expected = FAR[name]
    assert run["pushed"] == expected and run["arrival_pages"] == int(expected > 0), diagnostic
    assert run["quiet_stable_samples"] >= 3 and run["after_more_stable_samples"] >= 3, diagnostic
    assert abs(run["quiet"]["offset"] - before["position"]["offset"]) <= 2, diagnostic
    assert run["quiet"]["from_end"] > 600, diagnostic
    assert abs(run["after_more"]["offset"] - run["quiet"]["offset"]) <= 2, diagnostic
    assert run["horizon_at"] >= 6.3, diagnostic
    assert run["horizon_at"] - run["last_arrival_at"] >= 2.5, diagnostic
    assert run["horizon"]["from_end"] > 600, diagnostic
    assert abs(run["horizon"]["offset"] - before["position"]["offset"]) <= 2, diagnostic
    assert run["horizon_max_drift"] <= 2, diagnostic


def opening_trace(run):
    """Classify a failed unpaused opening from its own capture: catch-up state at the
    opening sample, each suspension release, the presented geometry and the first
    scroll that brought the revealed view toward the end."""
    trace, geometry = run.get("trace", []), run.get("geometry", [])
    releases, suspended = [], False
    for entry in trace:
        if "suspended" not in entry:
            continue
        if suspended and not entry["suspended"]:
            releases.append({"at": entry["at"], "trigger": entry["event"], "position": entry.get("position")})
        suspended = entry["suspended"]
    requests = run.get("requests", [])
    landing = None
    if requests:
        landing = next((g for g in geometry if g["at"] >= requests[-1]["at"]), None)
    unexpected = None
    if landing:
        later = [g for g in geometry if g["at"] > landing["at"]]
        for previous, current in zip([landing] + later, later):
            toward_end = previous["position"].get("from_end", 0) - current["position"].get("from_end", 0)
            if toward_end > END or current["position"].get("from_end", 1e9) <= DISTANT:
                decision = [e for e in trace if e["at"] <= current["at"] and e["event"] == "move"]
                source = decision[-1] if decision and decision[-1]["move"] == "jump" else None
                unexpected = {"at": current["at"], "from": previous["position"], "to": current["position"],
                              "source": source or "no follow jump recorded before it"}
                break
    return {"readiness_at_open": run.get("readiness_at_open"), "readiness_at_fold": run.get("readiness_at_fold"),
            "opened_at": run.get("opened_at"), "requests": requests, "completions": run.get("completions"),
            "suspension_releases": releases, "reveal_landing": landing,
            "first_unexpected_scroll": unexpected, "geometry": geometry}


@pytest.mark.parametrize("delay", ["250", "100"])
def test_distant_search_reveal_during_catchup_keeps_position(distant_reveals, delay):
    """Settling-follow coverage; the stream begins after reveal consumption."""
    run = distant_reveals[f"open_reveal_{delay}"]
    # A failure dumps its own classification trace (the 100 ms case's historical
    # 24 pt landing was never captured with one).
    diagnostic = json.dumps({"failure_trace": opening_trace(run), "run": run}, sort_keys=True)
    assert run["caught_up"] and run["reveal_consumed"] and run["notices"] == 2, diagnostic
    assert run["after_open"]["from_end"] > 600, diagnostic
    assert abs(run["after_stream"]["offset"] - run["after_open"]["offset"]) <= 2, diagnostic


def test_completion_before_scroll_cannot_release_a_distant_opening_reveal(fix8_view_binary, tmp_path):
    """Round 6: a loaded 250 ms opening recorded both reveal completions while the clip still
    showed the old end. The geometry confirmation released suspension on that geometry, and
    catch-up's next settling jump undid the reveal (24 pt from the end). This replays that
    order: every request's completion is delivered before its scroll is presented."""
    result = run_probe(fix8_view_binary, tmp_path, "fix10-completion-first", timeout=240)
    write_json(tmp_path / "result.json", result)
    run = result["open_reveal_completion_first"]
    diagnostic = json.dumps({"failure_trace": opening_trace(run), "run": run}, sort_keys=True)
    print("completion before scroll: " + diagnostic)
    assert result["visible_windows"] == 0, diagnostic
    # The order under test: the first completion arrives with the clip still at the end. The
    # probe's gate (round 8) puts it there in the request's own turn: mount-time scheduling had
    # let a loaded opening's pin still be settling (69.5 pt in round 7, 24 pt in review r6).
    early = run["early_completions"]
    assert run["completion_first"] and len(early) == 2, diagnostic
    assert "gated_from" in early[0] and "gated_from" not in early[1], diagnostic
    assert early[0]["position"]["from_end"] <= END, diagnostic
    assert run["caught_up"] and run["reveal_consumed"] and run["notices"] == 2, diagnostic
    assert run["after_open"]["from_end"] > 600, diagnostic
    assert abs(run["after_stream"]["offset"] - run["after_open"]["offset"]) <= 2, diagnostic
    # A distant reveal is never confirmed as leaving the end visible.
    assert not any(e["event"] == "reveal_end_visible" for e in run["trace"]), diagnostic


# The shrink replay's layouts (round 8's continuation): the probe's default, and larger text
# (1.728x, six View > Bigger steps), at which round 7's fixed 9,000 pt shrink failed as CI run
# 40303045129 did: `shrink.clamped` expired with the clip 116 pt from the end in 4 of 4 runs at
# 35ce3c745, and 283 pt from it in the landed control's one run.
LAYOUTS = {"default": {}, "larger_text": {"R219_PROBE_TEXT_SCALE": "1.728"}}


def resize_schedule(binary, root, kind, deltas):
    write_json(root / "schedule.json", {"kind": kind, "deltas": deltas})
    result = run_probe(binary, root, "fix10-resize-schedule", timeout=60)
    write_json(root / "resize-result.json", result)
    print("resize schedule: " + json.dumps(result, sort_keys=True))
    return result


def assert_resize_order(result):
    assert result["visible_windows"] == 0, result
    prepared = result["prepared"]
    if result["kind"] == "growth":
        assert prepared["own_landed"] == 1 and prepared["position"]["from_end"] == 0, result
    else:
        assert prepared["landed"] == int(result["kind"] == "landed"), result
        assert prepared["visible"] == 0 and prepared["position"]["from_end"] > DISTANT, result
    for resize in result["resizes"]:
        assert resize["revisions"] == int(resize["before"]["content"] != resize["applied"]["content"]), result
        assert resize["applied"]["content"] == max(800, resize["before"]["content"] + resize["delta"]), result


def assert_resize_reveal_invariant(result):
    """Every resize preserves an outstanding reveal; after landing, a clamped end
    is confirmed independently of whether the lazy marker remounts."""
    for resize in result["resizes"]:
        assert resize["landed"] == int(result["kind"] == "landed"), result
        if result["kind"] == "pending":
            assert resize["visible"] == 0, result
        elif resize["after"]["from_end"] == 0:
            assert resize["visible"] > 0, result
            if resize["revisions"]:
                assert resize["confirmations"] > 0, result


def assert_growth_resizes_follow(result):
    assert result["kind"] == "growth", result
    for resize in result["resizes"]:
        assert abs(resize["after"]["from_end"]) <= 0.5, result


def test_scripted_growth_is_followed_before_a_compensating_shrink(fix8_view_binary, tmp_path):
    result = resize_schedule(fix8_view_binary, tmp_path, "growth", [600, -900, 1300])
    assert_resize_order(result)
    assert_growth_resizes_follow(result)


@pytest.mark.parametrize("kind", ["pending", "landed"])
def test_scripted_document_resizes_without_a_marker_remount(fix8_view_binary, tmp_path, kind):
    result = resize_schedule(fix8_view_binary, tmp_path, kind, [-11000, 2000, -2500, 1000])
    assert_resize_order(result)
    assert_resize_reveal_invariant(result)


@settings(max_examples=12, deadline=None, derandomize=True, phases=(Phase.generate,))
@given(kind=st.sampled_from(["pending", "landed"]),
       deltas=st.lists(st.integers(-2500, 2500), min_size=0, max_size=8))
def test_reveal_invariant_for_resize_schedules(fix8_view_binary, tmp_path_factory, kind, deltas):
    root = tmp_path_factory.mktemp("resize-property")
    result = resize_schedule(fix8_view_binary, root, kind, [-11000, *deltas])
    assert_resize_order(result)
    assert_resize_reveal_invariant(result)


def shrink_replay(binary, root, mode, layout="default", *, remount_marker=False):
    """Round 7's schedule (`shrinkReplay` in the probe), with its own steps and trace, its shrink
    measured since round 8's continuation. A failed wait's whole report (steps, traces, layout)
    is printed, so CI's log keeps it."""
    load = os.getloadavg()[0]
    try:
        environment = dict(LAYOUTS[layout])
        if remount_marker:
            environment["R219_PROBE_REMOUNT_END"] = "1"
        result = run_probe(binary, root, f"fix10-shrink-{mode}", env=environment, timeout=240)
    except AssertionError:
        failure = root / "probe-failure.json"
        if failure.is_file():
            print(f"shrink {mode} probe failure: " + failure.read_text())
        raise
    write_json(root / "result.json", result)
    diagnostic = json.dumps(result, sort_keys=True)
    print(f"shrink {mode}: " + diagnostic)
    assert result["visible_windows"] == 0, diagnostic
    assert result["held_before_reveal"], diagnostic
    steps = {step["step"]: step for step in result["steps"]}
    if mode == "landed":
        print(f"landed layout={layout}, reveals={steps['landed']['reveals']}, load={load:.2f}")
    assert steps["opened"]["position"]["from_end"] <= END, diagnostic
    # Catch-up stays held until the settling tick.
    for name, step in steps.items():
        if step["at"] < steps["tick"]["at"]:
            assert not step["readiness"]["caught_up"] and step["readiness"]["catchup_pending"], diagnostic
    return result, steps, diagnostic


def releases_after(result, at):
    """Each trace entry from `at` on that turns suspension off."""
    return [e for e in result["trace"] if e["at"] >= at and "suspended" in e and not e["suspended"]
            and e["event"] != "move"]


def settling_ticks(result, steps):
    """Every settling follow decision after catch-up was released."""
    return [e for e in result["trace"] if e["at"] >= steps["tick"]["at"] and e["event"] == "move" and e["settling"]]


def settling_tick(result, steps):
    """The first settling follow decision after catch-up was released."""
    return next(iter(settling_ticks(result, steps)), None)


def assert_catchup_follows_the_end(result, steps):
    """Released, every settling tick jumps to the end, and the view is there once caught up. A
    sample between a page's growth and its jump can lag the end (83 pt, run 2026-10-26), so
    in-flight positions are not checked against END."""
    diagnostic = json.dumps(result, sort_keys=True)
    ticks = settling_ticks(result, steps)
    assert ticks and all(not t["suspended"] and t["move"] == "jump" for t in ticks), diagnostic
    assert steps["caught_up"]["position"]["from_end"] <= END, diagnostic


def assert_measured_shrinks(result, steps):
    """Each shrink was measured in its own turn: at least the clip's distance from the end plus a
    viewport (CI run 40303045129: a fixed 9,000 pt shrink left the clip 239 pt from the end), and
    the scene stopped shrinking once the end stayed in view."""
    diagnostic = json.dumps(result, sort_keys=True)
    shrinks = steps["shrunk"]["shrinks"]
    assert shrinks and steps["shrinking"]["position"]["from_end"] > DISTANT, diagnostic
    for shrink in shrinks:
        before = shrink["before"]
        assert shrink["amount"] >= before["from_end"] + before["viewport"], diagnostic
    assert all(s["before"]["from_end"] > 1 for s in shrinks), diagnostic


def assert_replayed_shrink_order(result, steps):
    """The CI order, each step as presented: the order the rule is checked against. The reveal is
    still outstanding at the shrink (the view's own trace; the readiness `reveal_pending` is the
    search palette's, false throughout this mode)."""
    diagnostic = json.dumps(result, sort_keys=True)
    assert steps["early_completion"]["position"]["from_end"] <= END, diagnostic
    assert steps["scrolled"]["position"]["from_end"] > DISTANT, diagnostic
    assert steps["shrinking"]["reveal"]["outstanding"], diagnostic
    assert steps["shrunk"]["reveal"]["outstanding"], diagnostic
    assert_measured_shrinks(result, steps)
    assert steps["shrunk"]["position"]["from_end"] <= END, diagnostic
    assert any(e["event"] == "reveal_check" and e["distance"] <= END
               and steps["shrinking"]["at"] <= e["at"] <= steps["shrunk"]["at"]
               for e in result["trace"]), diagnostic
    assert steps["late_completion"]["position"]["from_end"] <= END, diagnostic
    assert steps["regrown"]["position"]["from_end"] > DISTANT, diagnostic
    assert settling_tick(result, steps) is not None, diagnostic


def assert_shrink_releases_nothing(result, steps):
    """Neither the marker nor the completion at the clamped end released suspension, and the
    settling tick never brought the view to the end."""
    diagnostic = json.dumps(result, sort_keys=True)
    assert not releases_after(result, steps["reveal"]["at"]), diagnostic
    assert not any(e["event"] == "reveal_end_visible" for e in result["trace"]), diagnostic
    tick = settling_tick(result, steps)
    assert tick["suspended"] and tick["move"] == "stay", diagnostic
    for g in result["geometry"]:
        if g["at"] >= steps["regrown"]["at"]:
            assert g["position"]["from_end"] > DISTANT, json.dumps(g) + ": " + diagnostic
    for key in ("after_tick", "caught_up"):
        assert steps[key]["position"]["from_end"] > DISTANT, diagnostic


@pytest.mark.parametrize("layout", LAYOUTS)
def test_document_shrink_under_a_pending_reveal_releases_nothing(fix8_view_binary, tmp_path, layout):
    """Round 7 (CI run 40251214771, py3.12): the completion-first opening's reveal scrolled away
    from the end before its row was presented, and lazy measurement then shrank the document
    6,382 pt under the clip, clamping it to the end. `marker_appeared` and the request's own
    completion (`reveal_end_visible`) both released suspension, and catch-up's settling jump
    undid the reveal. This replays that order: while a reveal is outstanding, an end shown only
    because the document shrank releases nothing."""
    result, steps, _ = shrink_replay(fix8_view_binary, tmp_path, "replay", layout)
    assert_replayed_shrink_order(result, steps)
    assert_shrink_releases_nothing(result, steps)


def test_person_scroll_to_the_end_during_a_pending_reveal_releases(fix8_view_binary, tmp_path):
    """Round 7's control: the person scrolling to the end during the same reveal releases
    suspension, and catch-up's settling tick follows the end."""
    result, steps, diagnostic = shrink_replay(fix8_view_binary, tmp_path, "person")
    assert steps["scrolled"]["position"]["from_end"] > DISTANT, diagnostic
    assert steps["person_at_end"]["position"]["from_end"] <= END, diagnostic
    assert releases_after(result, steps["scrolled"]["at"]), diagnostic
    assert_catchup_follows_the_end(result, steps)


def assert_landed_shrink_order(result, steps):
    """The control's order: the reveal has landed on its row before the measured shrink."""
    diagnostic = json.dumps(result, sort_keys=True)
    assert steps["landed"]["stable"] >= 3 and steps["landed"]["position"]["from_end"] > DISTANT, diagnostic
    reveal = steps["shrinking"]["reveal"]
    assert not reveal["outstanding"] and reveal["ended_by"] == "reveal_landed", diagnostic
    assert_measured_shrinks(result, steps)
    assert steps["shrunk"]["position"]["from_end"] <= END, diagnostic
    assert steps["regrown"]["position"]["from_end"] > DISTANT, diagnostic
    assert settling_tick(result, steps) is not None, diagnostic


def assert_landed_shrink_releases(result, steps):
    """After landing, an end shown by a shrink releases suspension through either
    the marker or measured geometry, and catch-up's settling tick follows."""
    diagnostic = json.dumps(result, sort_keys=True)
    released = releases_after(result, steps["shrinking"]["at"])
    assert released and released[0]["event"] in {"marker_appeared", "reveal_end_visible"}, diagnostic
    assert_catchup_follows_the_end(result, steps)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_document_shrink_after_a_distant_reveal_lands_releases(fix8_view_binary, tmp_path, layout):
    """Round 7's control: once the reveal has landed on its row, none is outstanding, and an end
    shown by the same shrink releases suspension even without a marker remount."""
    result, steps, diagnostic = shrink_replay(fix8_view_binary, tmp_path, "landed", layout)
    assert steps["landed"]["reveals"] == 1, diagnostic
    assert_landed_shrink_order(result, steps)
    assert_landed_shrink_releases(result, steps)


def test_focused_50ms_probe_lands_and_keeps_position(fix8_view_binary, tmp_path):
    """Allow fresh-process repetitions without rerunning six unrelated scenes."""
    result = run_probe(fix8_view_binary, tmp_path, "fix10-far-text-50ms", timeout=240)
    write_json(tmp_path / "result.json", result)
    print("focused distant reveal positions: " + json.dumps(result, sort_keys=True))
    assert result["visible_windows"] == 0
    assert result["mounted_windows"] == 1 and result["animation_calls"] > 0
    test_distant_search_reveal_lands_and_keeps_position(result, "far_text_50ms")


@pytest.fixture(scope="module")
def streaming_reveals(fix8_view_binary, tmp_path_factory):
    root = tmp_path_factory.mktemp("fix10-streaming")
    result = run_probe(fix8_view_binary, root, "fix10-streaming", timeout=600)
    write_json(root / "result.json", result)
    print("streaming reveal positions: " + json.dumps(result, sort_keys=True))
    assert result["visible_windows"] == 0
    assert result["mounted_windows"] == 17 and result["animation_calls"] > 0
    return result


def presented_geometry(run):
    """Every presented clip geometry: bounds changes as they happen, and the 25 ms samples."""
    samples = [{"at": g["at"], "position": g["position"], "source": "bounds"} for g in run["geometry"]]
    samples += [{"at": e["at"], "position": e["position"], "source": e["event"]}
                for e in run["timeline"] if e["event"] in ("position", "presented")]
    return sorted((s for s in samples if "offset" in s["position"]), key=lambda s: s["at"])


def reveal_destination(run):
    """The final reveal request's first presented geometry: its own scroll, else the next sample."""
    requested = (run.get("requested_at") or run["notices_at"])[-1]
    after = [s for s in presented_geometry(run) if requested <= s["at"] <= run["landed_at"]]
    return next((s for s in after if s["source"] == "bounds"), after[0] if after else None)


def assert_reveal_reaches_destination(run):
    """(a) The reveal presents a distant destination before landing is trusted. The row
    is this scene's destination; the conversation's end is not."""
    diagnostic = json.dumps(run, sort_keys=True)
    destination = reveal_destination(run)
    assert destination is not None, "no presented geometry after the final request: " + diagnostic
    assert destination["position"]["from_end"] > DISTANT, \
        "reveal presented at " + json.dumps(destination) + ": " + diagnostic


def assert_reveal_interval_holds(run):
    """(b) Every presented geometry from the destination to the landing baseline stays more
    than DISTANT from the end and comes at most REVEAL_HOLD closer to it than the destination
    did. While the follow is engaged (not suspended, end visible), the view stays within END
    of the end."""
    diagnostic = json.dumps(run, sort_keys=True)
    destination = reveal_destination(run)
    follow = sorted((e for e in run["timeline"] if "suspended" in e), key=lambda e: e["at"])
    for sample in presented_geometry(run):
        if not destination["at"] <= sample["at"] <= run["landed_at"]:
            continue
        approach = destination["position"]["from_end"] - sample["position"]["from_end"]
        detail = json.dumps({"destination": destination, "sample": sample, "approach": approach}) + ": " + diagnostic
        state = [e for e in follow if e["at"] <= sample["at"]]
        if state and not state[-1]["suspended"] and state[-1]["at_bottom"]:
            assert sample["position"]["from_end"] <= END, "an engaged follow left the end: " + detail
        assert approach <= REVEAL_HOLD, "the view moved toward the end before landing: " + detail
        assert sample["position"]["from_end"] > DISTANT, "the view reached the end before landing: " + detail


def assert_stream_retains_position(run, *, events, pages, at_least=False):
    diagnostic = json.dumps(run, sort_keys=True)
    assert run["found"] and run["caught_up"] and run["reveal_consumed"], diagnostic
    # The landing baseline's own checkpoint: caught up, with the reveal consumed.
    landing = run["readiness_at_landing"]
    assert landing["caught_up"] and not landing["reveal_pending"], diagnostic
    assert len(run["notices_at"]) == 2 and run["reveal_starts"] == 1, diagnostic
    # Validate the reveal and the whole interval before trusting its landing baseline.
    assert_reveal_reaches_destination(run)
    assert_reveal_interval_holds(run)
    assert not run["producer_deadline_exceeded"], diagnostic
    if at_least:
        assert run["pushed"] >= events and run["arrival_pages"] == run["pushed"], diagnostic
    else:
        assert run["pushed"] == events and run["arrival_pages"] == pages, diagnostic
    assert len(run["arrivals_at"]) == len(run["folds_at"]) == run["pushed"], diagnostic
    assert run["folded_seq"] == run["target_seq"], diagnostic
    assert run["landed_stable_samples"] >= 3, diagnostic
    assert run["landed"]["from_end"] > 600, diagnostic
    for key in ("quiet", "after_more", "horizon"):
        assert run[key]["from_end"] > 600, diagnostic
        assert run[key]["offset"] == run["landed"]["offset"], diagnostic
    assert run["horizon_at"] >= 6.3 and run["horizon_max_drift"] == 0, diagnostic


@pytest.mark.parametrize("kind", ["text", "tool"])
@pytest.mark.parametrize("delay_ms", [0, 16, 50, 120])
def test_stream_arrives_before_first_reveal_notice(streaming_reveals, kind, delay_ms):
    run = streaming_reveals[f"early_{kind}_{delay_ms}ms"]
    diagnostic = json.dumps(run, sort_keys=True)
    assert_stream_retains_position(run, events=1, pages=1)
    assert run["arrivals_at"][0] >= delay_ms / 1000, diagnostic
    assert run["arrivals_at"][0] <= run["folds_at"][0] < run["notices_at"][0], diagnostic


def check_background_stream(run, *, events):
    diagnostic = json.dumps(run, sort_keys=True)
    assert_stream_retains_position(run, events=events, pages=events, at_least=True)
    first, second = run["notices_at"]
    assert run["arrivals_at"][0] < first < second <= run["consumed_at"] < run["arrivals_at"][-1], diagnostic
    assert any(first < at < second for at in run["arrivals_at"]), diagnostic
    assert run["folds_at"][-1] > run["consumed_at"], diagnostic


@pytest.mark.parametrize("kind,events", [("text", 50), ("tool", 17)])
def test_background_stream_crosses_both_notices_and_consumption(streaming_reveals, kind, events):
    check_background_stream(streaming_reveals[f"background_{kind}"], events=events)


# The checks that must reject a reveal which jumps away from its destination
# before landing is captured (review r5's surviving overlap-loss replay).
REVEAL_CHECKS = {"assert_reveal_reaches_destination", "assert_reveal_interval_holds"}


def test_background_checks_reject_replayed_overlap_jump(fix8_view_binary, tmp_path):
    """Review r5's survivor as a fixed schedule: 50 events, 15 between the notices
    and four after consumption, with its 177-pt-short jump injected after consumption."""
    result = run_probe(fix8_view_binary, tmp_path, "fix10-streaming-replayed_jump", timeout=240)
    write_json(tmp_path / "result.json", result)
    run = result["replayed_jump"]
    diagnostic = json.dumps(run, sort_keys=True)
    print("replayed overlap jump: " + diagnostic)
    assert result["visible_windows"] == 0, diagnostic
    first, second = run["notices_at"]
    consumed = run["consumed_at"]
    assert run["replay"] and run["pushed"] == 50 and run["arrival_pages"] == 50, diagnostic
    assert sum(first < at < second for at in run["arrivals_at"]) == 15, diagnostic
    assert sum(at > consumed for at in run["arrivals_at"]) == 4, diagnostic
    jump = run["injected_jump"]
    assert jump and jump["to"]["offset"] - jump["from"]["offset"] > 3000, diagnostic
    assert jump["to"]["from_end"] <= 200 and jump["at"] > consumed, diagnostic
    # The survivor's signature: it lands more than 600 pt from the end and then
    # holds with zero drift, so every pre-round-6 position check accepts it.
    assert run["landed"]["from_end"] > 600 and run["horizon_max_drift"] == 0, diagnostic
    with pytest.raises(AssertionError) as rejected:
        check_background_stream(run, events=50)
    assert rejected.traceback[-1].name in REVEAL_CHECKS, rejected.getrepr(style="short")


def test_trailing_stream_waits_for_late_reveal_consumption(streaming_reveals):
    """The independent cadence continues through a slow consumption turn."""
    run = streaming_reveals["late_consumption_tool"]
    diagnostic = json.dumps(run, sort_keys=True)
    assert_stream_retains_position(run, events=17, pages=17, at_least=True)
    assert run["consumption_pause"] == 3 and run["consumed_at"] > 2.5, diagnostic
    # Preserve the original 16-event independent prefix before consumption;
    # the ongoing stream additionally supplies at least three later ticks.
    assert run["arrivals_at"][15] < run["consumed_at"], diagnostic
    assert sum(at > run["consumed_at"] for at in run["arrivals_at"]) >= 3, diagnostic
    first, second = run["notices_at"]
    assert run["arrivals_at"][0] < first < second <= run["consumed_at"] < run["arrivals_at"][-1], diagnostic
    assert any(first < at < second for at in run["arrivals_at"]), diagnostic
    assert run["folds_at"][-1] > run["consumed_at"], diagnostic


@pytest.mark.parametrize("kind", ["text", "tool"])
@pytest.mark.parametrize("phase", ["first_notice", "second_notice", "consumption"])
def test_stream_burst_at_reveal_boundary_keeps_position(streaming_reveals, kind, phase):
    run = streaming_reveals[f"burst_{kind}_{phase}"]
    diagnostic = json.dumps(run, sort_keys=True)
    assert_stream_retains_position(run, events=10, pages=1)
    boundary = run["consumed_at"] if phase == "consumption" else run["notices_at"][int(phase == "second_notice")]
    assert run["boundary_arrival_at"] == boundary, diagnostic


@pytest.mark.parametrize("kind,events", [("text", 50), ("tool", 17)])
def test_stale_end_marker_cannot_undo_search_reveal(fix8_view_binary, tmp_path, kind, events):
    result = run_probe(fix8_view_binary, tmp_path, f"fix10-streaming-stale_{kind}", timeout=240)
    write_json(tmp_path / "result.json", result)
    run = result[f"stale_{kind}"]
    diagnostic = json.dumps(run, sort_keys=True)
    assert result["visible_windows"] == 0, diagnostic
    assert run["stale_marker"], diagnostic
    assert any(entry["event"] == "move" and entry["at_bottom"] and entry["suspended"]
               and not entry["settling"] for entry in run["timeline"]), diagnostic
    assert_stream_retains_position(run, events=events, pages=events, at_least=True)


@pytest.mark.parametrize("kind,events", [("text", 50), ("tool", 17)])
def test_background_stream_survives_delayed_first_notice(fix8_view_binary, tmp_path, kind, events):
    """A paused presentation must not exhaust the independent background input."""
    name = f"delayed_background_{kind}"
    result = run_probe(fix8_view_binary, tmp_path, f"fix10-streaming-{name}", timeout=240)
    write_json(tmp_path / "result.json", result)
    run = result[name]
    print("delayed background: " + json.dumps(run, sort_keys=True))
    diagnostic = json.dumps(run, sort_keys=True)
    assert result["visible_windows"] == 0, diagnostic
    assert run["first_notice_pause"] == 3, diagnostic
    assert_stream_retains_position(run, events=events, pages=events, at_least=True)
    first, second = run["notices_at"]
    assert run["arrivals_at"][0] < first < second <= run["consumed_at"] < run["arrivals_at"][-1], diagnostic
    assert any(first < at < second for at in run["arrivals_at"]), diagnostic
    assert run["folds_at"][-1] > run["consumed_at"], diagnostic
