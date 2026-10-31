"""Review #284: execute retained-request boundaries against the real AppKit pin."""
import json
import platform
import subprocess

import pytest

from tests.frontend.conftest import needs_swift, run_probe
from tests.frontend.swift import ROOT, app_sources

pytestmark = needs_swift


@pytest.fixture(scope="module")
def pin_inputs(tmp_path_factory):
    binary = tmp_path_factory.mktemp("review-pin") / "probe"
    command = ["xcrun", "swiftc", "-Xfrontend", "-disable-sandbox", "-parse-as-library", "-target", f"{platform.machine()}-apple-macos14.0",
               str(ROOT / "app/Sources/ConversationOpeningPin.swift"),
               str(ROOT / "tests/frontend/ReviewPR284PinProbe.swift"), "-o", str(binary)]
    built = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if built.returncode:
        raise RuntimeError(built.stderr)
    try:
        result = run_probe(binary, timeout=60)
    except AssertionError as error:
        raise RuntimeError("pin probe setup failed") from error
    print("REVIEW_PIN_INPUTS " + json.dumps(result, sort_keys=True))
    (binary.parent / "inputs.json").write_text(json.dumps(result, sort_keys=True))
    return {(r["action"], r["flipped"]): r for r in result}


@pytest.mark.parametrize("flipped", [True, False])
def test_retained_request_callback_boundaries(pin_inputs, flipped):
    for action in ["person", "abandon", "own_send", "detach"]:
        assert pin_inputs[action, flipped]["landed"] == 1
        assert pin_inputs[action, flipped]["callbacks"] == 0
    for action in ["unrelated_abandon", "target_removed"]:
        assert pin_inputs[action, flipped]["callbacks"] >= 1
    threshold = pin_inputs["threshold", flipped]
    assert threshold["at26"] == 0 and threshold["first"] == 1
    guarded = pin_inputs["guard", flipped]
    assert guarded["callbacks"] and guarded["releases"] == 0
    assert pin_inputs["fallback", flipped]["releases"] >= 1


@pytest.mark.parametrize("flipped", [True, False])
def test_repeat_callbacks_coalesce_and_do_not_move_the_pin(pin_inputs, flipped):
    repeat = pin_inputs["repeat", flipped]
    assert repeat["callbacks"] - repeat["first"] == 10
    assert repeat["offset_before"] == repeat["offset_after"]
    coalesced = pin_inputs["coalesce", flipped]
    assert coalesced["callbacks"] - coalesced["first"] == 1


def test_growth_after_person_input_and_a_quiet_send_is_pinned(pin_inputs):
    growth = pin_inputs["growth_after_input", True]
    assert growth["own_landed"] == 1
    assert growth["growth_gap"] == 0 and growth["reply_gap"] == 0, growth


@pytest.mark.parametrize("flipped", [True, False])
def test_conversation_change_cancels_the_landed_request(pin_inputs, flipped):
    reused = pin_inputs["identity", flipped]
    assert reused["landed"] == 1
    assert reused["callbacks"] == 0, reused


def test_production_sources_typecheck_without_view_test_names():
    command = ["xcrun", "swiftc", "-Xfrontend", "-disable-sandbox", "-typecheck", "-whole-module-optimization", "-parse-as-library",
               "-target", f"{platform.machine()}-apple-macos14.0", *map(str, app_sources())]
    built = subprocess.run(command, capture_output=True, text=True, timeout=900)
    assert built.returncode == 0, built.stderr[-12000:]


def test_identity_reset_mutant_is_rejected(tmp_path):
    source = (ROOT / "app/Sources/ConversationOpeningPin.swift").read_text()
    old = """    var conversationID: String? {
        didSet {
            guard conversationID != oldValue else { return }
            outstanding = nil
            landedRequest = nil
        }
    }
"""
    assert source.count(old) == 1
    path = tmp_path / "ConversationOpeningPin.swift"
    path.write_text(source.replace(old, "    var conversationID: String?\n"))
    binary = tmp_path / "probe"
    built = subprocess.run(["xcrun", "swiftc", "-Xfrontend", "-disable-sandbox", "-parse-as-library", str(path),
                            str(ROOT / "tests/frontend/ReviewPR284PinProbe.swift"), "-o", str(binary)],
                           capture_output=True, text=True, timeout=900)
    assert built.returncode == 0, built.stderr
    inputs = {(r["action"], r["flipped"]): r for r in run_probe(binary, timeout=60)}
    for flipped in [True, False]:
        for regression in [test_conversation_change_cancels_the_landed_request,
                           test_conversation_change_cancels_the_pending_request]:
            with pytest.raises(AssertionError):
                regression(inputs, flipped)


@pytest.mark.parametrize("flipped", [True, False])
def test_queued_checks_read_the_current_requests(pin_inputs, flipped):
    for action in ["queued_identity", "queued_pending_identity"]:
        reused = pin_inputs[action, flipped]
        assert reused["callbacks"] == 0 and reused["landed_after"] == reused["landed"], reused
    for action in ["same_identity", "same_pending_identity"]:
        current = pin_inputs[action, flipped]
        assert current["landed_after"] == 1 and current["callbacks"] >= 1, current
        assert current["same_request"], current


@pytest.mark.parametrize("flipped", [True, False])
def test_conversation_change_cancels_the_pending_request(pin_inputs, flipped):
    reused = pin_inputs["pending_identity", flipped]
    assert reused["landed"] == 0
    assert reused["callbacks"] == 0, reused


def test_pending_identity_gap_is_also_present_at_base(tmp_path):
    base = (ROOT / "tests/fixtures/qt-flake/pr284-base-opening-pin.swift.txt").read_text()
    old = "    var onRevealLanded: (UUID) -> Void = { _ in }"
    assert base.count(old) == 2
    base = base.replace(old, old + "\n    var onLandedEndVisible: ((UUID) -> Void)? = nil")
    path = tmp_path / "ConversationOpeningPin.swift"
    path.write_text(base)
    binary = tmp_path / "probe"
    built = subprocess.run(["xcrun", "swiftc", "-Xfrontend", "-disable-sandbox", "-parse-as-library",
                            str(path), str(ROOT / "tests/frontend/ReviewPR284PinProbe.swift"), "-o", str(binary)],
                           capture_output=True, text=True, timeout=900)
    assert built.returncode == 0, built.stderr
    result = run_probe(binary, timeout=60)
    pending = [r for r in result if r["action"] == "pending_identity"]
    assert len(pending) == 2 and all(r["callbacks"] > 0 and r["conversation"] == "B" for r in pending), result
    assert all(r["callbacks"] == 0 for r in result if r["action"] == "identity"), result


def test_person_scroll_cancellation_mutant_is_rejected(tmp_path):
    source = (ROOT / "app/Sources/ConversationOpeningPin.swift").read_text()
    old = "        landedRequest = nil\n        onPersonInput()"
    assert source.count(old) == 1
    source = source.replace(old, "        onPersonInput()")
    path = tmp_path / "ConversationOpeningPin.swift"
    path.write_text(source)
    binary = tmp_path / "probe"
    built = subprocess.run(["xcrun", "swiftc", "-Xfrontend", "-disable-sandbox", "-parse-as-library",
                            str(path), str(ROOT / "tests/frontend/ReviewPR284PinProbe.swift"), "-o", str(binary)],
                           capture_output=True, text=True, timeout=900)
    assert built.returncode == 0, built.stderr
    result = run_probe(binary, timeout=60)
    for flipped in [True, False]:
        [person] = [r for r in result if r["action"] == "person" and r["flipped"] == flipped]
        assert person["landed"] == 1
        with pytest.raises(AssertionError):
            assert person["callbacks"] == 0, person


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="Listed-P3-quiet-reply: quiet-landing cases still accept a compensating shrink")
def test_quiet_landing_rule_rejects_a_measured_missed_growth(tmp_path):
    from tests.frontend.test_review_r219_fix8_mutations import MUTATIONS, round10_rule

    source = (ROOT / "app/Sources/ConversationOpeningPin.swift").read_text()
    _, edits = MUTATIONS["quiet_landing_ends_following"]
    for old, new in edits:
        if source.count(old) != 1:
            raise RuntimeError("quiet-landing mutant no longer applies")
        source = source.replace(old, new)
    path = tmp_path / "ConversationOpeningPin.swift"
    path.write_text(source)
    binary = tmp_path / "probe"
    built = subprocess.run(["xcrun", "swiftc", "-Xfrontend", "-disable-sandbox", "-parse-as-library",
                            str(path), str(ROOT / "tests/frontend/ReviewPR284PinProbe.swift"), "-o", str(binary)],
                           capture_output=True, text=True, timeout=900)
    if built.returncode:
        raise RuntimeError(built.stderr)
    try:
        result = run_probe(binary, timeout=60)
    except AssertionError as error:
        raise RuntimeError("quiet-landing mutant setup failed") from error
    [growth] = [r for r in result if r["action"] == "growth_after_input"]
    if not (growth["own_landed"] == 1 and growth["growth_gap"] == 600 and growth["reply_gap"] == 0):
        raise RuntimeError(f"quiet-landing counterexample setup failed: {growth}")
    sample = {"steps": [{"step": "reply", "position": {"from_end": growth["reply_gap"]}}]}
    # Quiet-landing cases do not attach the new optional resize_probe. Their old
    # reply-only blind spot remains even though the growth-observer mutant is fixed.
    try:
        round10_rule("quiet-regrow")(sample, {"reply": sample["steps"][0]})
    except AssertionError:
        return
    raise AssertionError(f"measured missed growth was accepted after compensation: {growth}")


def test_release_probe_counts_suspension_transitions():
    from tests.frontend.test_review_r219_fix10_view import releases_after

    result = {"trace": [{"event": "marker_appeared", "at": 0, "suspended": False},
                        {"event": "reveal_end_visible", "at": 2, "suspended": False}]}
    assert not releases_after(result, 1), "no suspension was released after t=1"


@pytest.mark.parametrize("at,expected", [(0, [2, 5]), (1, [2, 5]), (2, [2, 5]), (3, [5]), (6, [])])
def test_release_probe_keeps_state_across_the_cutoff(at, expected):
    from tests.frontend.test_review_r219_fix10_view import releases_after

    result = {"trace": [
        {"event": "marker_appeared", "at": 0, "suspended": False},
        {"event": "reveal", "at": 1, "suspended": True},
        {"event": "reveal_check", "at": 1.5},
        {"event": "reveal_end_visible", "at": 2, "suspended": False},
        {"event": "reveal_end_visible", "at": 3, "suspended": False},
        {"event": "reveal", "at": 4, "suspended": True},
        {"event": "move", "at": 5, "suspended": False},
    ]}
    assert [e["at"] for e in releases_after(result, at)] == expected
