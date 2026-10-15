"""Reproduce #219 r1's nine mutants and the three round-2 additions.

Each source copy is independent; each pytest runs synchronously under its own
shared lock. No production source, uv cache setting, or live state is changed.
Use --list to inspect the named tests without running anything.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TRAY = "app/Sources/QueueTray.swift"
STORE = "app/Sources/ConversationStore.swift"
VIEW = "app/Sources/UIQueueTray.swift"
CORE_TEST = "tests/frontend/test_core_queue_tray.py::"
ENGINE_TEST = "tests/frontend/test_core_queue_engine.py::"
VIEW_TEST = "tests/frontend/test_queue_tray_view.py::"
REVIEW_TEST = "tests/frontend/test_review_r219_probes.py::"


@dataclass(frozen=True)
class Mutant:
    name: str
    path: str
    before: str
    after: str
    test: str


MEMBERSHIP = "if (ahead || deferred) && turn.continues == nil && !turn.sentAsSteer { out.append(id) }"
MUTANTS = [
    Mutant("flicker", TRAY, MEMBERSHIP,
           "if (ahead || deferred || turn.state == \"queued\") && turn.continues == nil && !turn.sentAsSteer { out.append(id) }",
           CORE_TEST + "test_c29_7_an_idle_send_answered_queued_stays_in_the_timeline"),
    Mutant("tombstone", "app/Sources/Timeline.swift",
           "guard Timeline.withdrawnBeforeStart(turn) else { return nil }", "return nil",
           CORE_TEST + "test_c29_7_a_send_withdrawn_before_its_receipt_keeps_its_line_after_a_tombstone"),
    Mutant("waiting-continuation", TRAY,
           "displayOrder.filter { turn($0).map(Timeline.waitsInQueue) ?? false }",
           "displayOrder.filter { turn($0).map { Timeline.waitsInQueue($0) && $0.continues == nil } ?? false }",
           CORE_TEST + "test_c29_7_a_person_send_waits_behind_a_queued_failover_continuation"),
    Mutant("expanded-sizing", VIEW,
           "ViewThatFits(in: .vertical) {\n                    list(rows)\n                    ScrollView { list(rows) }.frame(height: 200)\n                }",
           "ScrollView { list(rows) }.frame(height: 200)",
           VIEW_TEST + "test_c29_7_an_expanded_tray_whose_rows_fit_is_as_tall_as_its_rows"),
    Mutant("M17", STORE, "case .waiting, .starting, .running, .approvalNeeded:\n            do {",
           "case .starting, .running, .approvalNeeded:\n            do {",
           ENGINE_TEST + "test_c29_7_a_message_that_became_its_own_live_turn_is_interrupted[waiting]"),
    Mutant("M19", STORE,
           "case .withdrawn(let receipt), .stopped(let receipt): return receipt\n            case .inFlight: return nil",
           "case .withdrawn(let receipt), .stopped(let receipt): return receipt\n            case .inFlight: return try cancel(messageID)",
           ENGINE_TEST + "test_c29_7_stop_withdraw_gives_what_withdraw_send_gives[in-flight]"),
    Mutant("O1", VIEW, "if busy {\n                ProgressView()",
           "if busy && !row.canSteer {\n                ProgressView()",
           VIEW_TEST + "test_c29_7_a_row_being_withdrawn_cannot_be_steered"),
    Mutant("wake-ordering", "app/Sources/Timeline.swift",
           'turn?.origin == "wake" ? 1 : 0, turn?.seq ?? Int.max)',
           "0, turn?.seq ?? Int.max)",
           CORE_TEST + "test_c29_7_property_order_matches_the_real_daemons_next_dispatchable"),
    Mutant("steering-as-own-turn", STORE,
           "case .steering, .steered:\n            throw ConversationEngineError.steerTooLate(messageID)\n        case .waiting, .starting, .running, .approvalNeeded:",
           "case .steered:\n            throw ConversationEngineError.steerTooLate(messageID)\n        case .steering, .waiting, .starting, .running, .approvalNeeded:",
           ENGINE_TEST + "test_c29_7_withdraw_of_a_message_in_another_turn_never_interrupts_its_host[steering]"),
    Mutant("steering-not-ahead", TRAY,
           "$0.messageState.map(MessageState.live.contains) ?? false",
           "$0.messageState != .steering && ($0.messageState.map(MessageState.live.contains) ?? false)",
           REVIEW_TEST + "test_r3b_every_dispatcher_live_state_holds_the_tray[steering]"),
    Mutant("missed-steer-in-tray", TRAY, MEMBERSHIP,
           "if (ahead || deferred) && turn.continues == nil && (!turn.sentAsSteer || turn.missedSteer) { out.append(id) }",
           REVIEW_TEST + "test_r2_a_missed_steer_stays_in_the_timeline"),
    Mutant("deferred-without-withdraw", TRAY,
           "withdraw = note ? .none : sending ? .withdraw(messageID: turn.messageID) : .cancel(messageID: turn.messageID)",
           'withdraw = note || turn.stateReason?.hasPrefix("deferred:") == true ? .none : sending ? .withdraw(messageID: turn.messageID) : .cancel(messageID: turn.messageID)',
           REVIEW_TEST + "test_r7_a_deferred_head_has_its_reason_and_withdraw_in_the_tray"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    selected = [m for m in MUTANTS if args.only is None or m.name in args.only]
    if args.only and set(args.only) - {m.name for m in MUTANTS}:
        parser.error("unknown mutation name")
    for mutant in selected:
        source = (ROOT / mutant.path).read_text()
        if source.count(mutant.before) != 1:
            raise ValueError(f"{mutant.name}: expected exactly one mutation anchor")
        print(f"{mutant.name}: {mutant.test}", flush=True)
    if args.list:
        return 0
    results = ROOT / "build/r219f2/mutations"
    results.mkdir(parents=True, exist_ok=True)
    temp_root = subprocess.check_output(["getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()
    for mutant in selected:
        with tempfile.TemporaryDirectory(prefix=f"sf-r219f2-{mutant.name}-", dir=temp_root) as scratch:
            copy = Path(scratch) / "source"
            copy.mkdir()
            for folder in ("app", "subfleet", "tests", "private"):
                shutil.copytree(ROOT / folder, copy / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for filename in ("pyproject.toml", "uv.lock", "README.md"):
                shutil.copy2(ROOT / filename, copy / filename)
            target = copy / mutant.path
            target.write_text(target.read_text().replace(mutant.before, mutant.after, 1))
            environment = dict(os.environ, TMPDIR=scratch + "/", HYPOTHESIS_STORAGE_DIRECTORY=str(Path(scratch) / "hyp"),
                               HYPOTHESIS_PROFILE="ci")
            result = subprocess.run(["/usr/bin/lockf", "-k", "/private/tmp/claude-501/subfleet-suites.lock",
                                     "uv", "run", "pytest", "-q", "-p", "no:cacheprovider", mutant.test],
                                    cwd=copy, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            (results / f"{mutant.name}.log").write_text(result.stdout)
            killed = result.returncode == 1 and "FAILED " in result.stdout and re.search(r"AssertionError|E\s+assert ", result.stdout)
            print(f"{mutant.name}: {'KILLED' if killed else 'NOT VERIFIED'}", flush=True)
            if not killed:
                print(result.stdout[-3000:])
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
