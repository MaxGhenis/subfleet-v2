"""Independent base replay and retained-request mutation for review #284."""
import pytest

from tests.frontend.conftest import needs_swift
from tests.frontend.swift import ROOT, app_sources, compile_probe
from tests.frontend.test_review_r219_fix8_view import fix8_view_binary
from tests.frontend.test_review_r219_fix10_view import (
    assert_growth_resizes_follow, assert_resize_order, assert_resize_reveal_invariant,
    resize_schedule, test_reveal_invariant_for_resize_schedules as resize_property,
    shrink_replay, assert_landed_shrink_order, assert_landed_shrink_releases,
)

pytestmark = needs_swift


def pin_variant(root, text):
    source = root / "ConversationOpeningPin.swift"
    source.write_text(text)
    sources = [source if p.name == source.name else p for p in app_sources()]
    return compile_probe(root / "probe", ROOT / "tests/frontend/ReviewR219Fix8ViewProbe.swift",
                         "SUBFLEET_VIEW_TEST", sources=sources, timeout=900)


def test_base_pin_resize_schedule_exposes_the_landed_race(tmp_path):
    base = (ROOT / "tests/fixtures/qt-flake/pr284-base-opening-pin.swift.txt").read_text()
    # Only the new API member is supplied for compilation with today's UIWindow.
    # The base never calls it; all pin execution remains exactly the base source.
    old = "    var onRevealLanded: (UUID) -> Void = { _ in }"
    assert base.count(old) == 2
    base = base.replace(old, old + "\n    var onLandedEndVisible: ((UUID) -> Void)? = nil")
    binary = pin_variant(tmp_path, base)
    for kind in ["pending", "landed", "growth"]:
        root = tmp_path / kind; root.mkdir()
        deltas = [600, -900, 1300] if kind == "growth" else [-11000, 2000, -2500, 1000]
        result = resize_schedule(binary, root, kind, deltas)
        assert_resize_order(result)
        if kind == "landed":
            with pytest.raises(AssertionError):
                assert_resize_reveal_invariant(result)
            assert all(r["visible"] == 0 for r in result["resizes"])
        elif kind == "pending":
            assert_resize_reveal_invariant(result)
        else:
            assert_growth_resizes_follow(result)
    # The baseline app already resumes a landed reader when its marker remounts.
    # Deliver that actual handler explicitly, independently of SwiftUI laziness.
    root = tmp_path / "marker-reader"; root.mkdir()
    result, steps, _ = shrink_replay(binary, root, "landed", replay_marker=True)
    assert_landed_shrink_order(result, steps)
    assert_landed_shrink_releases(result, steps)


def test_generated_property_rejects_dropping_the_landed_request(tmp_path, tmp_path_factory):
    source = (ROOT / "app/Sources/ConversationOpeningPin.swift").read_text()
    old = "            landedRequest = request\n"
    assert source.count(old) == 1
    binary = pin_variant(tmp_path, source.replace(old, "            landedRequest = nil\n"))
    with pytest.raises(AssertionError) as rejected:
        resize_property(fix8_view_binary=binary, tmp_path_factory=tmp_path_factory)
    assert rejected.traceback[-1].name == "assert_resize_reveal_invariant", rejected.getrepr(style="short")


def test_scripted_growth_control_passes_repeatedly(fix8_view_binary, tmp_path):
    for repeat in range(3):
        root = tmp_path / str(repeat); root.mkdir()
        result = resize_schedule(fix8_view_binary, root, "growth", [600, -900, 1300])
        assert_resize_order(result)
        assert_growth_resizes_follow(result)
