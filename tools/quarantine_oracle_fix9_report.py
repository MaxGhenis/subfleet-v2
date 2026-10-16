"""Record bounded, serial oracle controls without changing the existing tool.

Prepare the manifest without pytest using --prepare-only. Execute only while
holding the global pytest lease, through quarantine_fix9_run.py's wall bound.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time
import traceback


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/quarantine_oracle_mutations.py"
MODEL = ROOT / "tests/fake/test_quarantine_process_world.py"
IMMUTABLE = ("subfleet/procs.py", "subfleet/daemon.py", "tests/fake/test_state_contract.py",
             "tests/fake/test_quarantine_self_resolve.py", "tests/fake/quarantine_world_pool.py")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def immutable_hashes():
    return {name: digest((ROOT / name).read_bytes()) for name in IMMUTABLE}


def manifest_for(mutations, original):
    source = original.decode()
    rows = []
    for index, (name, old, new, node) in enumerate(mutations, 1):
        assert source.count(old) == 1, (name, source.count(old))
        filename, function = node.split("::", 1)
        function = function.split("[", 1)[0]
        tree = ast.parse((ROOT / filename).read_text())
        assert any(isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef)) and part.name == function
                   for part in tree.body), node
        rows.append({"id": index, "name": name, "old": old, "new": new, "node": node,
                     "anchor_occurrences": 1,
                     "mutant_sha256": digest(source.replace(old, new).encode())})
    return {"model_sha256": digest(original), "tool_sha256": digest(TOOL.read_bytes()),
            "immutable_source_sha256": immutable_hashes(), "mutations": rows}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def execute(module, manifest, selected, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    original = MODEL.read_bytes()
    before = digest(original)
    immutable_before = immutable_hashes()
    assert before == manifest["model_sha256"]
    rows = []
    globals_ = module["main"].__globals__
    original_run = globals_["run"]
    original_mutations = globals_["MUTATIONS"]
    calls = 0
    error = None
    code = 1

    def recorded_run(node):
        nonlocal calls
        entry = selected[calls // 2]
        role = "control" if calls % 2 == 0 else "mutant"
        calls += 1
        assert node == entry["node"]
        expected = before if role == "control" else entry["mutant_sha256"]
        assert digest(MODEL.read_bytes()) == expected, (entry["name"], role, "unexpected model bytes")
        stem = f"oracle-{entry['id']:02d}-{role}"
        stdout_path = evidence / (stem + "-stdout.txt")
        stderr_path = evidence / (stem + "-stderr.txt")
        started = time.monotonic()
        payload = {"id": entry["id"], "name": entry["name"], "node": node, "role": role,
                   "model_sha256": expected, "stdout": str(stdout_path.relative_to(ROOT)),
                   "stderr": str(stderr_path.relative_to(ROOT)), "returncode": None}
        print(f"RUN {stem}: {entry['name']}", flush=True)
        actual_subprocess_run = subprocess.run

        def spooled_run(*args, **kwargs):
            # Preserve the existing runner's exact command, environment and
            # 540-second bound. Stream capture to durable files, including
            # partial output if interruption unwinds subprocess.run.
            assert kwargs.pop("capture_output", False)
            assert kwargs.get("timeout") == 540
            with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
                result = actual_subprocess_run(*args, **kwargs, stdout=stdout, stderr=stderr)
            return subprocess.CompletedProcess(result.args, result.returncode,
                                               stdout_path.read_text(), stderr_path.read_text())

        subprocess.run = spooled_run
        try:
            result = original_run(node)
            payload["returncode"] = result.returncode
            payload["baseline_passed"] = role == "control" and result.returncode == 0
            payload["assertion_killed"] = role == "mutant" and result.returncode == 1 and any(
                label in result.stdout for label in ("DID NOT RAISE", "AssertionError"))
            return result
        except BaseException as exc:
            payload["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            subprocess.run = actual_subprocess_run
            payload["elapsed_seconds"] = round(time.monotonic() - started, 3)
            rows.append(payload)
            write_json(evidence / (stem + "-result.json"), payload)
            write_json(evidence / "oracle-runs.json", rows)

    globals_["run"] = recorded_run
    globals_["MUTATIONS"] = tuple((row["name"], row["old"], row["new"], row["node"]) for row in selected)
    try:
        code = module["main"]()
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        globals_["run"] = original_run
        globals_["MUTATIONS"] = original_mutations
        # The existing main restores too; retain this independent outer guard
        # when calling it by instrumentation rather than its __main__ entry.
        if MODEL.read_bytes() != original:
            MODEL.write_bytes(original)
        after = digest(MODEL.read_bytes())
        immutable_after = immutable_hashes()
        controls = sum(row.get("baseline_passed", False) for row in rows)
        killed = sum(row.get("assertion_killed", False) for row in rows)
        summary = {"requested_mutations": len(selected), "run_count": len(rows),
                   "baseline_controls_passed": controls, "assertion_mutants_killed": killed,
                   "returncode": code, "error": error, "model_sha256_before": before,
                   "model_sha256_after": after, "immutable_source_before": immutable_before,
                   "immutable_source_after": immutable_after}
        write_json(evidence / "oracle-summary.json", summary)
        table = ["| Mutation | Control | Mutant | Assertion killed |",
                 "| --- | --- | --- | --- |"]
        for entry in selected:
            control = next((row for row in rows if row["id"] == entry["id"] and row["role"] == "control"), {})
            mutant = next((row for row in rows if row["id"] == entry["id"] and row["role"] == "mutant"), {})
            table.append(f"| {entry['name']} | {control.get('returncode', 'pending')} | "
                         f"{mutant.get('returncode', 'pending')} | {mutant.get('assertion_killed', False)} |")
        (evidence / "oracle-table.md").write_text("\n".join(table) + "\n")
        assert before == after and immutable_before == immutable_after, "oracle run changed frozen sources"
        print(json.dumps(summary), flush=True)
    return int(code != 0 or controls != len(selected) or killed != len(selected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--manifest", default="docs/reports/pr131-fix9/oracle-manifest.json")
    parser.add_argument("--evidence", default="docs/reports/pr131-fix9/oracle-final")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--stop", type=int, default=10)
    args = parser.parse_args()
    manifest_path = (ROOT / args.manifest).resolve()
    evidence = (ROOT / args.evidence).resolve()
    assert manifest_path.is_relative_to(ROOT) and evidence.is_relative_to(ROOT)
    module = runpy.run_path(str(TOOL), run_name="fix9_oracle_tool")
    current = manifest_for(module["MUTATIONS"], MODEL.read_bytes())
    assert 1 <= args.start <= args.stop <= len(current["mutations"])
    if args.prepare_only:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(manifest_path, current)
        print(f"Prepared {len(current['mutations'])} unique anchors and existing control functions; no pytest ran.")
        print(f"Model SHA-256: {current['model_sha256']}")
        return 0
    assert json.loads(manifest_path.read_text()) == current, "prepared oracle manifest differs from frozen sources"
    return execute(module, current, current["mutations"][args.start - 1:args.stop], evidence)


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"oracle report interrupted by signal {signum}")
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    finally:
        signal.signal(signal.SIGTERM, previous)
