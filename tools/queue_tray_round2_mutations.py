"""Reproduce #219 r1's nine mutants and the three round-2 additions.

Each source copy is independent; each pytest runs synchronously under its own
shared lock. No production source, uv cache setting, or live state is changed.
Use --list to inspect the named tests without running anything.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
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


def source_signature() -> str:
    """Only reuse prepared probes for exactly the sources and tests copied."""
    signature = hashlib.sha256()
    for folder in ("app", "subfleet", "tests", "private"):
        for source in sorted((ROOT / folder).rglob("*")):
            if source.is_file() and source.suffix in (".py", ".swift"):
                signature.update(str(source.relative_to(ROOT)).encode())
                signature.update(source.read_bytes())
    for filename in ("pyproject.toml", "uv.lock", "tools/queue_tray_round2_mutations.py"):
        signature.update((ROOT / filename).read_bytes())
    for name in ("DEVELOPER_DIR", "SDKROOT", "SUBFLEET_SWIFT_NESTED_SANDBOX"):
        signature.update((name + "\0" + os.environ.get(name, "")).encode())
    return signature.hexdigest()


def copy_mutant(copy: Path, mutant: Mutant) -> None:
    copy.mkdir()
    for folder in ("app", "subfleet", "tests", "private"):
        shutil.copytree(ROOT / folder, copy / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for filename in ("pyproject.toml", "uv.lock", "README.md"):
        shutil.copy2(ROOT / filename, copy / filename)
    target = copy / mutant.path
    target.write_text(target.read_text().replace(mutant.before, mutant.after, 1))


def prepare(mutant: Mutant, destination: Path, signature: str, results: Path) -> None:
    """Compile in the foreground; pytest later uses its existing exact-input cache."""
    ready = destination / "ready"
    if ready.is_file() and ready.read_text() == signature:
        print(f"{mutant.name}: already prepared", flush=True)
        return
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    copy = destination / "source"
    copy_mutant(copy, mutant)
    if mutant.test.startswith(VIEW_TEST):
        inputs = "[ROOT / 'tests/frontend/QueueTrayViewProbe.swift']"
        flag = "SUBFLEET_VIEW_TEST"
    else:
        inputs = "CORE_PROBE"
        flag = "SUBFLEET_MODEL_TEST"
    code = ("from tests.frontend.conftest import CORE_PROBE; "
            "from tests.frontend.swift import ROOT, compile_probe; "
            "from pathlib import Path; "
            "binary=ROOT/'build/prepared/probe'; binary.parent.mkdir(parents=True, exist_ok=True); "
            f"compile_probe(binary, {inputs}, {flag!r}, timeout={900 if flag == 'SUBFLEET_VIEW_TEST' else 540})")
    python = ROOT / ".venv/bin/python"
    command = [str(python) if python.is_file() else sys.executable, "-c", code]
    with (results / f"{mutant.name}.prepare.log").open("w") as log:
        subprocess.run(command, cwd=copy, check=True, stdout=log, stderr=subprocess.STDOUT)
    ready.write_text(signature)
    print(f"{mutant.name}: prepared (no pytest run)", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--prepare", action="store_true", help="prepare isolated native probes without running pytest")
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
    prepared = ROOT / "build/r219f2/prepared"
    signature = source_signature()
    if args.prepare:
        for mutant in selected:
            if source_signature() != signature:
                print("Sources changed; stop preparation before the next mutant", flush=True)
                return 2
            prepare(mutant, prepared / mutant.name, signature, results)
        return 0
    temp_root = subprocess.check_output(["getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()
    for mutant in selected:
        ready = prepared / mutant.name / "ready"
        target = ready.parent / "source" / mutant.path
        try:
            use_prepared = (ready.read_text() == signature
                            and target.read_text() == (ROOT / mutant.path).read_text().replace(mutant.before, mutant.after, 1))
        except OSError:
            use_prepared = False
        with tempfile.TemporaryDirectory(prefix=f"sf-r219v2-{mutant.name}-", dir=temp_root) as scratch:
            copy = ready.parent / "source" if use_prepared else Path(scratch) / "source"
            if not use_prepared:
                copy_mutant(copy, mutant)
            environment = dict(os.environ, TMPDIR=scratch + "/", HYPOTHESIS_STORAGE_DIRECTORY=str(Path(scratch) / "hyp"),
                               HYPOTHESIS_PROFILE="ci")
            acquired = results / f"{mutant.name}.acquired"
            acquired.unlink(missing_ok=True)
            enter = ("import datetime,os,pathlib,sys; "
                     "pathlib.Path(sys.argv[1]).write_text(datetime.datetime.now(datetime.timezone.utc).isoformat()); "
                     "os.execvp(sys.argv[2],sys.argv[2:])")
            result = subprocess.run(["/usr/bin/lockf", "-k", "/private/tmp/claude-501/subfleet-suites.lock",
                                     sys.executable, "-c", enter, str(acquired),
                                     "uv", "run", "pytest", "-q", "-p", "no:cacheprovider", mutant.test],
                                    cwd=copy, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            (results / f"{mutant.name}.log").write_text(result.stdout)
            killed = result.returncode == 1 and "FAILED " in result.stdout and re.search(r"AssertionError|E\s+assert ", result.stdout)
            print(f"{mutant.name}: {'KILLED' if killed else 'NOT VERIFIED'}", flush=True)
            if acquired.is_file():
                hold = (datetime.now(timezone.utc) - datetime.fromisoformat(acquired.read_text())).total_seconds()
                print(f"{mutant.name}: lock hold {hold:.2f}s", flush=True)
            if not killed:
                print(result.stdout[-3000:])
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
