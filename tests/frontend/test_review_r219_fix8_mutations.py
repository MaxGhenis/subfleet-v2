"""Each fix-8 mutation must fail a named behavioral regression."""
from functools import partial
import json
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
    resize_schedule,
    assert_resize_order,
    assert_growth_resizes_follow,
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
from tests.frontend.test_review_r219_fix13_view import (
    assert_landed_once,
    assert_natural_presentation,
    assert_landing_follows,
    assert_quiet_landing_order,
    assert_rests_at_the_end,
    assert_stale_glide_order,
    landing_run,
    own_send_landings,
)

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
    # Round 10: following after an own send outlasts its landing check, whose quiet window stays.
    "quiet_landing_ends_following": ("ConversationOpeningPin.swift", [
        ("                self.ownSendLanding = false\n                self.onOwnSendLanded()",
         "                self.followingOwnSend = false\n                self.ownSendLanding = false\n"
         "                self.onOwnSendLanded()")]),
    "growth_repins_dropped": ("ConversationOpeningPin.swift", [
        ("                                             queue: .main) { [weak self] _ in\n            self?.schedulePin()",
         "                                             queue: .main) { [weak self] _ in\n"
         "            if self?.followingOwnSend != true { self?.schedulePin() }")]),
    "quiet_window_0ms": ("ConversationOpeningPin.swift", [
        ("DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: check)",
         "DispatchQueue.main.asyncAfter(deadline: .now(), execute: check)")]),
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
# Each round-10 mutant with each scene that must kill it: the order that scene must show
# (outside `raises`) and the rule it must fail.
ROUND10_CASES = [
    ("quiet_landing_ends_following", "quiet-regrow"),
    ("quiet_landing_ends_following", "quiet-regrow-natural"),
    ("quiet_landing_ends_following", "quiet-regrow-late"),
    ("growth_repins_dropped", "quiet-regrow"),
    ("quiet_window_0ms", "stale-glide"),
]
ROUND10 = {mutation for mutation, _ in ROUND10_CASES}
# A fixture mutant, separate from product mutations: omit this write from the
# barrier's corrected-gap trace so only the independent clipTo counter sees it.
FIXTURE_MUTATIONS = {
    "untraced_partial_correction": [
        ('            if record.steps.last?["step"] as? String == "landed" { return true }',
         '            if abs(gap) > correctionTolerance { record.clipTo(fromEnd: 26) }\n'
         '            if record.steps.last?["step"] as? String == "landed" { return true }'),
    ],
}
VIEW = {"notice_sets_at_bottom_false", "key_on_all_rows", "uimodel_drops_known_receipts",
        "settling_stops_follow_at_end", "own_send_latch_ends_at_marker", *ROUND7, *ROUND8, *ROUND10}


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
                                      and name not in ROUND8 and name != "own_send_latch_ends_at_marker"
                                      and name not in ROUND10])
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
    result, steps, _ = shrink_replay(binary, tmp_path, mode,
                                    remount_marker=mutation == "marker_releases_pending_reveal")
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
    # Round 10: the pin's following outlasts the landing check, so ending the check at the marker
    # no longer leaves the reply behind; the landing is then never measured.
    binary, _ = compile_mutation("own_send_latch_ends_at_marker", tmp_path)
    result, steps, _ = landing_run(binary, tmp_path, "regrow")
    assert "growing" in steps
    failed = []
    for rule in (assert_landing_follows, assert_landed_once):
        try:
            rule(result, steps)
        except AssertionError:
            failed.append(rule.__name__)
    assert failed, f"own_send_latch_ends_at_marker survived regrow: reply {steps['reply']['position']['from_end']} pt"
    print(f"own_send_latch_ends_at_marker killed by {', '.join(failed)}[regrow]")


def round10_order(mode):
    if mode.startswith("quiet-"):
        return lambda result, steps: assert_quiet_landing_order(result, steps, mode)
    if mode == "stale-glide":
        return assert_stale_glide_order
    return lambda result, steps: None


def round10_rule(mode):
    if mode == "stale-glide":
        return assert_rests_at_the_end
    return assert_growth_and_reply_follow


def assert_growth_and_reply_follow(result, steps):
    # A later shrink or the reply's ordinary follow jump can repair a missed
    # repin. Measure each scripted growth before either can mask it.
    if "resizes" in result:
        assert_growth_resizes_follow(result)
    if "resize_probe" in result:
        probe = result["resize_probe"]
        assert_growth_and_reply_follow(probe, {s["step"]: s for s in probe["steps"]})
    assert_landing_follows(result, steps)


def test_scripted_regrow_then_shrink_reaches_the_growth_mutant_check(tmp_path):
    binary, _ = compile_mutation("growth_repins_dropped", tmp_path)
    result = resize_schedule(binary, tmp_path, "growth", [600, -900])
    assert_resize_order(result)
    assert result["resizes"][0]["applied"]["from_end"] == 600, result
    steps = {step["step"]: step for step in result["steps"]}
    # The old reply-only rule accepts this mutant: shrink clamps the clip before
    # the reply sample. Only a check of the growth's own response can reject it.
    with pytest.raises(AssertionError):
        round10_rule("quiet-regrow")(result, steps)


@pytest.mark.parametrize("mutation,mode", ROUND10_CASES)
def test_round10_mutation_fails_its_named_regression(mutation, mode, tmp_path):
    binary, _ = compile_mutation(mutation, tmp_path)
    result, steps, _ = landing_run(binary, tmp_path, mode)
    round10_order(mode)(result, steps)
    if mutation == "growth_repins_dropped":
        result["resize_probe"] = resize_schedule(binary, tmp_path, "growth", [600, -900, 1300])
        assert_resize_order(result["resize_probe"])
    rule = round10_rule(mode)
    with pytest.raises(AssertionError) as rejected:
        rule(result, steps)
    print(f"{mutation} killed by {rule.__name__}[{mode}]: {str(rejected.value)[:300]}")


def test_ci_landing_gap_reaches_the_behavioural_mutant_check(tmp_path):
    """Replay r9's CI counterexample: a valid 24 pt callback must not kill the mutant
    through setup. Establish zero before growth, then reject its unfollowed reply.

    Replay the measured callback offset in the real scene, rather than depending on
    this host's lazy layout to happen to produce the CI offset again.
    """
    saved = json.loads((ROOT / "tests/fixtures/qt-flake/r9-ci-quiet-natural.json").read_text())
    saved_steps = {step["step"]: step for step in saved["steps"]}
    assert_natural_presentation(saved, saved_steps)
    gap = saved_steps["landed"]["landed"]["position"]["from_end"]
    assert gap == 24 and saved_steps["reply"]["position"]["from_end"] == 880
    # The saved scene still fails setup: the zero-gap barrier must remain strict.
    with pytest.raises(AssertionError):
        assert_quiet_landing_order(saved, saved_steps, "quiet-regrow-natural")

    source = (ROOT / "tests/frontend/ReviewR219Fix8ViewProbe.swift").read_text()
    old = '            self.trace.append(entry.merging(["at": self.at(), "position": self.position()]) { _, new in new })'
    assert source.count(old) == 1
    source = source.replace(old, f'''            if entry["event"] as? String == "own_send_landed" {{
                self.clipTo(fromEnd: {gap})
            }}
{old}''')
    probe = mutant_source("r11_ci_landing", "Probe.swift", source)
    filename, edits = MUTATIONS["quiet_landing_ends_following"]
    sources = []
    for path in app_sources():
        if path.name == filename:
            text = path.read_text()
            for before, after in edits:
                assert text.count(before) == 1
                text = text.replace(before, after)
            path = mutant_source("quiet_landing_ends_following", filename, text)
        sources.append(path)
    binary = compile_probe(tmp_path / "probe", probe, "SUBFLEET_VIEW_TEST", sources=sources)
    mode = "quiet-regrow-natural"
    result, steps, _ = landing_run(binary, tmp_path, mode)
    [callback] = own_send_landings(result, steps)
    assert callback["position"]["from_end"] == gap, result
    # Setup is deliberately outside raises. Only the reply assertion kills this mutant.
    assert_quiet_landing_order(result, steps, mode)
    with pytest.raises(AssertionError):
        assert_landing_follows(result, steps)
    print(f"CI replay reached behavioural check: callback={gap}, "
          f"quiet={steps['landed']['position']['from_end']}, reply={steps['reply']['position']['from_end']}")


@pytest.mark.parametrize("mutation", FIXTURE_MUTATIONS)
def test_round13_fixture_mutation_fails_the_full_view_counter(mutation, tmp_path, monkeypatch):
    from tests.frontend import test_review_r219_fix14_setup as setup

    source = setup.setup_source()
    for before, after in FIXTURE_MUTATIONS[mutation]:
        assert source.count(before) == 1, (mutation, before)
        source = source.replace(before, after)
    probe = mutant_source(mutation, "Probe.swift", source)
    binary = compile_probe(tmp_path / "probe", probe, "SUBFLEET_VIEW_TEST")
    run, result = setup.setup_run(binary, tmp_path, 650)
    # A compiler failure, another readiness failure, invalid callback/input or
    # broken zero-gap order must fail this test, never count as a counter kill.
    writes = setup.assert_distant_setup_input(run, result)
    assert writes and writes[0]["gap"] == 650, result
    context = result if run.returncode == 0 else result["context"]
    state = (next(step for step in context["steps"] if step["step"] == "landed")
             if run.returncode == 0 else result["state"])
    assert 650 not in state["zero_end_corrected_gaps"], result
    monkeypatch.setattr(setup, "setup_run", lambda *_: (run, result))
    with pytest.raises(AssertionError, match="fixture clipTo write outside 25 pt"):
        setup.test_quiet_setup_rejects_a_clip_650_pt_behind_after_valid_callback(binary, tmp_path)
    print(f"{mutation} killed by independent full-view counter: {writes}")
