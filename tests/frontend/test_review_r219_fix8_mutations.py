"""Each fix-8 mutation must fail a named behavioral regression."""
import os
import platform
import subprocess

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.swift import ROOT, app_sources
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

pytestmark = needs_swift
MUTATIONS = {
    "uimodel_drops_known_receipts": ("UIModel.swift", [
        ("let known = knownConversationID.map { state.knownMessageIDs(in: $0) } ?? []",
         "let known: Set<String> = []")]),
    "settling_stops_follow_at_end": ("QueueTray.swift", [
        ("suspended && !atBottom ? .stay : .jump", "suspended ? .stay : .jump")]),
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
}


def compile_mutation(mutation, tmp_path):
    filename, edits = MUTATIONS[mutation]
    sources = []
    for source in app_sources():
        if source.name == filename:
            text = source.read_text()
            for old, new in edits:
                assert text.count(old) == 1, (mutation, old)
                text = text.replace(old, new)
            source = tmp_path / filename
            source.write_text(text)
        sources.append(source)
    view = mutation in {"notice_sets_at_bottom_false", "key_on_all_rows",
                        "uimodel_drops_known_receipts", "settling_stops_follow_at_end"}
    probe = ROOT / f"tests/frontend/ReviewR219Fix8{'View' if view else 'Engine'}Probe.swift"
    binary = tmp_path / "probe"
    command = ["xcrun", "swiftc", "-whole-module-optimization", "-D",
               "SUBFLEET_VIEW_TEST" if view else "SUBFLEET_MODEL_TEST", "-parse-as-library",
               "-target", f"{platform.machine()}-apple-macos14.0", *map(str, sources), str(probe), "-o", str(binary)]
    if os.environ.get("SUBFLEET_SWIFT_NESTED_SANDBOX") == "off":
        command[2:2] = ["-Xfrontend", "-disable-sandbox"]
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=900)
    assert compiled.returncode == 0, compiled.stderr[-20000:]
    return binary, view


@pytest.mark.parametrize("mutation", [name for name in MUTATIONS if name != "settling_stops_follow_at_end"])
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
    assert control["suspended"] and control["at_bottom"], control
    assert control["after_page"]["from_end"] > 600, control
    print(f"settling mutant {name}: {control['after_page']['from_end']} pt from end")
    with pytest.raises(AssertionError):
        catchup_regression(settling_mutant, name)
    print(f"settling mutant {name} killed by {catchup_regression.__name__}")
