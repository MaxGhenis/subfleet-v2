"""Each fix-8 mutation must fail a named behavioral regression."""
from functools import partial
import os

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.swift import ROOT, app_sources, compile_probe
from tests.frontend.test_review_r219_fix8_engine import (
    test_reopen_fetches_no_known_receipt as reopen_regression,
    test_receipt_batch_sorts_once as batch_regression,
)
from tests.frontend.test_review_r219_fix8_view import (
    END,
    test_timeline_reveal_leaving_end_visible_keeps_follow as follow_regression,
    test_continuous_rows_reveal_at_most_twice as reveal_regression,
)

from tests.frontend.test_review_r219_fix9_view import (
    test_real_uimodel_reopen_fetches_no_known_receipt as uimodel_regression,
    test_live_row_reveal_during_slow_catchup_keeps_the_end as catchup_regression,
)
from tests.frontend.test_review_r219_fix10_view import (
    assert_landed_shrink_order,
    assert_landed_shrink_releases,
    assert_replayed_shrink_order,
    assert_shrink_releases_nothing,
    shrink_replay,
)
from tests.frontend.test_review_r219_fix12_view import (
    assert_mark_probes_order,
    assert_mark_probes_run_the_check,
    assert_missing_away_order,
    assert_missing_away_releases,
    assert_own_send_order,
    assert_own_send_releases,
    fix12_run,
)
from tests.frontend.test_review_r219_fix13_view import landing_run, assert_landing_follows

pytestmark = needs_swift
MUTATIONS = {
    "uimodel_drops_known_receipts": ("UIModel.swift", [
        ("let known = knownConversationID.map { state.knownMessageIDs(in: $0) } ?? []",
         "let known: Set<String> = []")]),
    "settling_stops_follow_at_end": ("QueueTray.swift", [
        ("suspended ? .stay : .jump", ".stay")]),
    "notice_sets_at_bottom_false": ("UIWindow.swift", [
        ("            followSuspended = true\n", "            followSuspended = true\n            atBottom = false\n")]),
    "hydrate_full_history": ("ConversationStore.swift", [
        ("let known = knownMessageIDs.union(open.messages.map(\\.message_id))",
         "let known = Set(open.messages.map(\\.message_id))")]),
    "sort_per_receipt": ("Timeline.swift", [
        ("        turns[receipt.message_id] = turn\n", "        turns[receipt.message_id] = turn\n        sortOrder()\n")]),
    "key_on_all_rows": ("UISearchPalette.swift", [
        ("            var targetPresent: Bool\n", "            var targetPresent: Bool\n            var allRows: Set<String>?\n"),
        ("targetPresent: present)", "targetPresent: present, allRows: revealIDs)")]),
    # Round 7: an outstanding reveal's suspension against an end shown by a shrink.
    "marker_releases_pending_reveal": ("UIWindow.swift", [
        ("atBottom = true; if !revealPending { followSuspended = false }", "atBottom = true; followSuspended = false")]),
    "confirm_by_distance_only": ("ConversationOpeningPin.swift", [
        ("if presented && distance <= Theme.space.column + 1", "if distance <= Theme.space.column + 1")]),
    "reveal_never_lands": ("UIWindow.swift", [
        ("        revealPending = false\n        traceFollow(\"reveal_landed\")", "        traceFollow(\"reveal_landed\")")]),
    # Round 8: abandonment, the own send during catch-up, and where the mark is checked.
    "abandon_keeps_pending": ("UIWindow.swift", [
        ("        revealPending = false\n        ConversationOpeningPin.abandonReveal(in: conversation.conversation_id, "
         "request: request)\n", "")]),
    "own_send_after_catchup_guard": ("UIWindow.swift", [
        ("let ownSend = FollowKey.ownSend(from: old, to: new)",
         "let ownSend = !(old.settling || new.settling) && FollowKey.ownSend(from: old, to: new)")]),
    "mark_local_frame_only": ("ConversationOpeningPin.swift", [
        ("watchPlacement(window != nil)", "watchPlacement(false)"),
        ("var itemID: String? { didSet { if itemID != oldValue { placeAgain() } } }", "var itemID: String?")]),
    "own_send_latch_ends_at_marker": ("UIWindow.swift", [
        ('        traceFollow("marker_appeared")',
         '        ownSendLanding = false\n        traceFollow("marker_appeared")')]),
}
# Each round-7 mutant, the replay mode it runs, the order that mode must show (checked
# outside `raises`, so a broken schedule cannot pass as a kill) and the rule it must fail.
ROUND7 = {
    "marker_releases_pending_reveal": ("replay", assert_replayed_shrink_order, assert_shrink_releases_nothing),
    "confirm_by_distance_only": ("replay", assert_replayed_shrink_order, assert_shrink_releases_nothing),
    "reveal_never_lands": ("landed", assert_landed_shrink_order, assert_landed_shrink_releases),
}
# Each round-8 mutant, its scene, the order that scene must show (outside `raises`) and the
# rule it must fail.
ROUND8 = {
    "abandon_keeps_pending": ("missing-away", assert_missing_away_order, assert_missing_away_releases),
    "own_send_after_catchup_guard": ("send-catchup", partial(assert_own_send_order, catchup=True),
                                     partial(assert_own_send_releases, catchup=True)),
    "mark_local_frame_only": ("mark", lambda result, _: assert_mark_probes_order(result),
                              lambda result, _: assert_mark_probes_run_the_check(result)),
}
VIEW = {"notice_sets_at_bottom_false", "key_on_all_rows", "uimodel_drops_known_receipts",
        "settling_stops_follow_at_end", "own_send_latch_ends_at_marker", *ROUND7, *ROUND8}


def mutant_source(mutation, filename, text):
    """The mutated file at a path of its own, so `compile_probe` reuses an exact earlier build
    (a full-source compile can take most of a hold on a loaded host)."""
    path = ROOT / "build/frontend-mutants" / mutation / filename
    if path.is_file() and path.read_text() == text:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{filename}.{os.getpid()}.partial")
    partial.write_text(text)
    os.replace(partial, path)
    return path


def compile_mutation(mutation, tmp_path):
    filename, edits = MUTATIONS[mutation]
    sources = []
    for source in app_sources():
        if source.name == filename:
            text = source.read_text()
            for old, new in edits:
                assert text.count(old) == 1, (mutation, old)
                text = text.replace(old, new)
            source = mutant_source(mutation, filename, text)
        sources.append(source)
    view = mutation in VIEW
    probe = ROOT / f"tests/frontend/ReviewR219Fix8{'View' if view else 'Engine'}Probe.swift"
    binary = compile_probe(tmp_path / "probe", probe, "SUBFLEET_VIEW_TEST" if view else "SUBFLEET_MODEL_TEST",
                           timeout=900, sources=sources)
    return binary, view


@pytest.mark.parametrize("mutation", [name for name in MUTATIONS
                                      if name != "settling_stops_follow_at_end" and name not in ROUND7
                                      and name not in ROUND8 and name != "own_send_latch_ends_at_marker"])
def test_mutation_fails_its_named_regression(mutation, tmp_path):
    binary, view = compile_mutation(mutation, tmp_path)
    if mutation == "uimodel_drops_known_receipts":
        mode = "fix9-reopen"
        result = run_probe(binary, tmp_path, mode, timeout=600)
        regression, args = uimodel_regression, (result,)
    elif view:
        result = run_probe(binary, tmp_path, timeout=600)
        regression = follow_regression if mutation == "notice_sets_at_bottom_false" else reveal_regression
        args = (result,) if mutation == "notice_sets_at_bottom_false" else (result, "busy_400ms")
    else:
        result = run_probe(binary, write_json(tmp_path / "input.json", {"histories": [500], "batch": 64}), timeout=600)
        regression = reopen_regression if mutation == "hydrate_full_history" else batch_regression
        args = (result, 500) if mutation == "hydrate_full_history" else (result,)
    with pytest.raises(AssertionError):
        regression(*args)
    print(f"{mutation} killed by {regression.__name__}")


@pytest.fixture(scope="module")
def settling_mutant(tmp_path_factory):
    root = tmp_path_factory.mktemp("settling-mutant")
    binary, _ = compile_mutation("settling_stops_follow_at_end", root)
    result = run_probe(binary, root, "fix9-catchup", timeout=600)
    write_json(root / "result.json", result)
    return result


@pytest.mark.parametrize("name", ["open_live_250", "open_live_100"])
def test_settling_mutation_fails_each_named_regression(settling_mutant, name):
    # Each schedule is a separate pytest item: a surviving mutant on one must
    # never prevent the other from being evaluated. Setup failures are outside
    # raises, so they cannot masquerade as a rejected behavioral mutation.
    run = settling_mutant[name]
    assert run["held_before_reveal"], run
    assert run["before_catchup"]["from_end"] <= END, run
    assert run["after_initial_reveal"]["from_end"] <= END, run
    assert run["caught_up"] and run["reveal_consumed"] and run["notices"] == 2, run
    control = run["settling_control"]
    assert control["reveals"] == 2 and control["consumed"], control
    assert control["before_page"]["from_end"] <= END, control
    assert control["changed_key"] and control["settling"], control
    assert control["confirmations"] >= 1 and not control["suspended"] and control["at_bottom"], control
    assert control["after_page"]["from_end"] > 600, control
    print(f"settling mutant {name}: {control['after_page']['from_end']} pt from end")
    with pytest.raises(AssertionError):
        catchup_regression(settling_mutant, name)
    print(f"settling mutant {name} killed by {catchup_regression.__name__}")


@pytest.mark.parametrize("mutation", ROUND7)
def test_round7_mutation_fails_its_named_regression(mutation, tmp_path):
    binary, _ = compile_mutation(mutation, tmp_path)
    mode, order, rule = ROUND7[mutation]
    result, steps, _ = shrink_replay(binary, tmp_path, mode)
    order(result, steps)
    with pytest.raises(AssertionError) as rejected:
        rule(result, steps)
    print(f"{mutation} killed by {rule.__name__}: {str(rejected.value)[:300]}")


@pytest.mark.parametrize("mutation", ROUND8)
def test_round8_mutation_fails_its_named_regression(mutation, tmp_path):
    binary, _ = compile_mutation(mutation, tmp_path)
    mode, order, rule = ROUND8[mutation]
    result, steps, _ = fix12_run(binary, tmp_path, mode)
    order(result, steps)
    with pytest.raises(AssertionError) as rejected:
        rule(result, steps)
    print(f"{mutation} killed by {getattr(rule, 'func', rule).__name__}: {str(rejected.value)[:300]}")


def test_round9_marker_latch_mutation_fails_regrow(tmp_path):
    binary, _ = compile_mutation("own_send_latch_ends_at_marker", tmp_path)
    result, steps, _ = landing_run(binary, tmp_path, "regrow")
    assert "growing" in steps
    with pytest.raises(AssertionError) as rejected:
        assert_landing_follows(result, steps)
    print(f"own_send_latch_ends_at_marker killed by assert_landing_follows[regrow]: {str(rejected.value)[:300]}")
