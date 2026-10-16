"""Bounded, process-free replay of the CI property and its shared-fixture oracle.

Run in the foreground with a fresh Darwin-user TMPDIR. ``--legacy`` executes
the exact saved a026a52f5 property; the default executes the corrected oracle.
No provider or daemon process is launched. A SIGALRM bounds the whole replay,
including minimization, and every fixture is closed before its directory goes.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CI_VERSION = "6.168.1"
CI_BLOB = b"AXicY3RkZnRkZ3RkZXRkYgSxGcBsdjBiYwAALSwCwg=="
CI_ACTIONS = ["reuse1", "restart", "verifiable", "reuse0", "reuse1", "exit0",
              "verifiable", "restart", "restart", "tick"]
ORACLE_SEQUENCES = [["kill"], ["exit0", "reuse1", "tick"]]


def temp_parent() -> Path:
    parent = Path(os.environ["TMPDIR"]).resolve()
    darwin = Path(subprocess.check_output(
        ["getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip()).resolve()
    if parent == darwin or not parent.is_relative_to(darwin) or "tmp" in parent.parts:
        raise ValueError("TMPDIR must be a fresh child of DARWIN_USER_TEMP_DIR, without a tmp component")
    return parent


def replay(sequences: list[list[str]], *, legacy: bool, blob: bool = False) -> dict:
    import pytest
    from hypothesis import reproduce_failure, settings
    from hypothesis.errors import DidNotReproduce
    from subfleet import procs
    from tests.fake import test_quarantine_self_resolve as current
    from tests.fake.test_state_contract import state_daemon

    proofs = []
    original = current.ORIGINAL_CENSUS

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        # Read the simulation's writer truth from the calling property's
        # census closure, independently of the production census verdict.
        frame = inspect.currentframe().f_back
        states = list(frame.f_locals["states"])
        unavailable = frame.f_locals["unavailable"]
        del frame
        proofs.append({"attempt_id": args[3], "verified_empty": result.verified_empty,
                       "live_pids": sorted(result.live_pids), "errors": list(result.errors),
                       "writer_states": states, "inspection_unavailable": unavailable})
        return result

    if legacy:
        namespace = {**vars(current), "ORIGINAL_CENSUS": observed}
        saved = ROOT / "docs/reports/pr131-fix9/ci-property-before.py"
        exec(compile(saved.read_text(), str(saved), "exec"), namespace)
        decorated = namespace["test_property_release_requires_every_recorded_writer_gone_and_occurs_at_most_once"]
        run = decorated.hypothesis.inner_test
    else:
        decorated = current.test_property_release_requires_every_recorded_writer_gone_and_occurs_at_most_once
        run = current.release_property_sequence

    result = {"legacy": legacy, "sequences": sequences, "ci_blob": blob,
              "failed": False, "proofs": proofs}
    with tempfile.TemporaryDirectory(prefix="pr131-replay-", dir=temp_parent()) as directory:
        patch = pytest.MonkeyPatch()
        fixture = state_daemon.__wrapped__(Path(directory), patch)
        daemon, harness = next(fixture)
        if not legacy:
            patch.setattr(current, "ORIGINAL_CENSUS", observed)
        try:
            for actions in sequences:
                run((daemon, harness), patch, actions)
            if blob:
                # Reproduction draws the exact saved buffer; cap any fallback
                # generation too, and keep a real per-example deadline.
                exact = reproduce_failure(CI_VERSION, CI_BLOB)(decorated)
                exact._hypothesis_internal_use_settings = settings(
                    exact._hypothesis_internal_use_settings, max_examples=1,
                    deadline=60000, database=None)
                exact(state_daemon=(daemon, harness), monkeypatch=patch)
        except DidNotReproduce as exc:
            # @reproduce_failure deliberately raises when its saved buffer
            # completes successfully. That is the expected after-fix outcome,
            # rather than an assertion failure or a rejected buffer.
            result["failed"] = legacy or not blob
            result["saved_failure_absent"] = not result["failed"]
            result["reproduction_exception"] = type(exc).__name__
            result["reproduction_message"] = str(exc)
        except Exception as exc:
            result["failed"] = True
            result["exception"] = type(exc).__name__
            result["message"] = str(exc)
        finally:
            result["attempts"] = [{"attempt_id": a["attempt_id"], "state": a["state"]}
                                  for a in daemon.store.list_attempts()]
            result["releases"] = [{"attempt_id": e["attempt_id"], "kind": e["kind"],
                                    "containment": json.loads(e["data_json"])["containment"]}
                                   for e in daemon.store.query(
                                       "SELECT * FROM events WHERE kind IN "
                                       "('quarantine.self_resolved','quarantine.confirmed_dead')")]
            try:
                next(fixture)
            except StopIteration:
                pass
            finally:
                patch.undo()
    # The release event's own evidence shows whether a genuine unsafe release
    # occurred; a last-proof failure alone cannot establish one.
    result["unsafe_release"] = any(
        e["containment"]["unverifiable"] or e["containment"]["live_pids"]
        or next(p for p in reversed(proofs) if p["attempt_id"] == e["attempt_id"])["inspection_unavailable"]
        or "live" in next(p for p in reversed(proofs) if p["attempt_id"] == e["attempt_id"])["writer_states"]
        for e in result["releases"])
    return result


def minimize(sequences: list[list[str]], *, legacy: bool) -> tuple[list[list[str]], int]:
    """Delete actions until no single deletion preserves the oracle failure."""
    current = [list(actions) for actions in sequences]
    checks = 0
    if not replay(current, legacy=legacy)["failed"]:
        raise ValueError("candidate does not reproduce an assertion failure")
    changed = True
    while changed:
        changed = False
        for index in range(len(current)):
            for action in range(len(current[index])):
                candidate = [list(actions) for actions in current]
                del candidate[index][action]
                candidate = [actions for actions in candidate if actions]
                checks += 1
                if replay(candidate, legacy=legacy)["failed"]:
                    current, changed = candidate, True
                    break
            if changed:
                break
    return current, checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--mode", choices=("blob", "actions", "oracle"), default="oracle")
    parser.add_argument("--warmup", action="store_true", help="record an older holding attempt before the CI replay")
    parser.add_argument("--minimize", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1500:
        parser.error("timeout must be between 1 and 1500 seconds")

    def expired(signum, frame):
        raise TimeoutError(f"replay exceeded {args.timeout} seconds")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(args.timeout)
    started = time.monotonic()
    try:
        sequences = ORACLE_SEQUENCES if args.mode == "oracle" else (
            ([["kill"]] if args.warmup else []) + ([CI_ACTIONS] if args.mode == "actions" else []))
        if args.minimize:
            if args.mode == "blob":
                parser.error("minimize explicit actions or the shared-fixture oracle")
            sequences, checks = minimize(sequences, legacy=args.legacy)
        else:
            checks = 0
        result = replay(sequences, legacy=args.legacy, blob=args.mode == "blob")
        result.update(elapsed_s=round(time.monotonic() - started, 3), minimization_checks=checks,
                      timeout_s=args.timeout, hypothesis_version=CI_VERSION)
        output = json.dumps(result, indent=2) + "\n"
        if args.output:
            destination = args.output.resolve()
            if not destination.is_relative_to(ROOT):
                parser.error("output must be inside the assigned workspace")
            destination.write_text(output)
        print(output, end="")
        return int(result["failed"])
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
