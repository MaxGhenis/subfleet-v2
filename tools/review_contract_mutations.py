"""Run D-REVIEW-CONTRACT1's five mutants, restoring pristine source bytes.

Run after the ordinary tests, while nothing else edits or tests these sources:
    python3 tools/review_contract_mutations.py

Only the selected unit tests run. Each mutant gets its own pytest process and
scratch directory; logs, pristine copies, commands, hashes and results remain
under /private/tmp/sf-rv-a1/mutations. A collection error is not a killed mutant.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutant:
    name: str
    source: str
    before: str
    after: str
    killer: str


MUTANTS = (
    Mutant(
        "preamble-all-jobs", "subfleet/daemon.py",
        '            review = args.task == "review" or args.kind == "gate-review"\n',
        '            review = True  # mutation: every job is treated as a review\n',
        "tests/unit/test_review_contract_prompt.py::test_non_review_prompts_never_get_the_review_contract",
    ),
    Mutant(
        "level-anywhere-in-line", "subfleet/review_result.py",
        '        level = _finding_level(line)\n',
        '        level = (match.group(0) if (match := __import__("re").search('
        'r"P[0-3](?=:|[ \\t]+[—-])", line)) else None)\n',
        "tests/unit/test_review_result.py::test_levels_in_prose_and_malformed_prefixes_are_not_findings",
    ),
    Mutant(
        "valid-ignores-verdict", "subfleet/review_result.py",
        '    if verdict is None:\n',
        '    if False:  # mutation: a missing verdict does not invalidate a round\n',
        "tests/unit/test_review_result.py::test_first_nonempty_line_must_be_a_verdict_even_with_findings",
    ),
    Mutant(
        "first-attempt-review", "subfleet/daemon.py",
        '                evidence["review"] = parse_review_result(payload.decode("utf-8", "replace"))\n',
        '                evidence["review"] = (json.loads(\n'
        '                    self.store.list_attempts(job["job_id"])[0]["evidence_json"] or "{}"\n'
        '                ).get("review") or parse_review_result(payload.decode("utf-8", "replace")))\n',
        "tests/unit/test_review_result_storage.py::test_retry_reports_only_accepted_attempt_review",
    ),
    Mutant(
        "p0-definition-shortened", "subfleet/daemon.py",
        'P0: harm that is live now or would be on merge (a leak, lost data, a guard that stops guarding).',
        'P0: harm that is live now (a leak, lost data, a guard that stops guarding).',
        "tests/unit/test_review_contract_prompt.py::test_preamble_definitions_are_verbatim_and_under_130_words",
    ),
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_private_pytest_plugin(case_dir: Path) -> str:
    """Redirect session-start pruning before pytest loads its conftest hooks."""
    name = "sf_rv_review_mutation"
    plugin = case_dir / f"{name}.py"
    plugin.write_text(
        "from pathlib import Path\n"
        "import tests.failed_keep\n"
        f"tests.failed_keep.ROOT = Path({str(case_dir / 'failed')!r})\n"
    )
    return name


def run_pytest(command: list[str], env: dict[str, str], timeout: float) -> tuple[int, str]:
    """Keep the foreground child bounded, including interruption cleanup."""
    child = subprocess.Popen(command, cwd=ROOT, env=env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        try:
            output, _ = child.communicate(timeout=timeout)
            return child.returncode, output
        except subprocess.TimeoutExpired:
            child.kill()
            output, _ = child.communicate()
            return 124, output + f"\nmutation test timed out after {timeout:g} seconds\n"
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()


def run_mutant(mutant: Mutant, pristine: dict[str, bytes], copies: Path,
               run_dir: Path, cache: Path, timeout: float) -> dict:
    # Each mutation begins with every source pristine, including files it does
    # not change. Compare bytes as well as retaining hashes for the report.
    for relative, original in pristine.items():
        assert (ROOT / relative).read_bytes() == original, (mutant.name, relative, "source changed")
        assert (copies / relative).read_bytes() == original, (mutant.name, relative, "copy changed")
    source = ROOT / mutant.source
    original = (copies / mutant.source).read_bytes()
    before, after = mutant.before.encode(), mutant.after.encode()
    assert original.count(before) == 1, (mutant.name, "anchor must occur exactly once")
    changed = original.replace(before, after, 1)
    compile(changed, str(source), "exec")
    case_dir = run_dir / mutant.name
    case_dir.mkdir()
    plugin = write_private_pytest_plugin(case_dir)
    command = ["uv", "--cache-dir", str(cache), "run", "--no-sync", "pytest", "-q", "-x",
               "-p", plugin, "-p", "no:cacheprovider",
               "--tb=short", mutant.killer, "--basetemp", str(case_dir / "pytest")]
    env = {**os.environ, "TMPDIR": str(case_dir), "UV_CACHE_DIR": str(cache),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": str(case_dir / "pycache"),
           "HYPOTHESIS_STORAGE_DIRECTORY": str(case_dir / "hypothesis"),
           "PYTHONPATH": os.pathsep.join(part for part in (
               str(case_dir), str(ROOT), os.environ.get("PYTHONPATH", "")) if part)}
    result = {"mutation": mutant.name, "source": mutant.source, "killer": mutant.killer,
              "command": command, "source_sha256": sha256(original),
              "mutant_sha256": sha256(changed)}
    started = time.monotonic()
    try:
        source.write_bytes(changed)
        assert source.read_bytes() == changed, (mutant.name, "mutation write differs")
        exit_code, output = run_pytest(command, env, timeout)
        log = case_dir / "pytest.log"
        log.write_text(output)
        failures = re.findall(r"^FAILED (.+?)(?: - .*)?$", output, re.MULTILINE)
        killed = exit_code == 1 and any(node.startswith(mutant.killer) for node in failures)
        result.update(exit_code=exit_code, killed=killed, failed_tests=failures,
                      summary=output.strip().splitlines()[-1:] or ["no output"],
                      log=str(log), seconds=round(time.monotonic() - started, 3))
    finally:
        # Do not overwrite an unexpected concurrent edit. The runner's caller
        # must keep these two sources idle for the duration of the five runs.
        current = source.read_bytes()
        assert current in (original, changed), (mutant.name, "unexpected concurrent edit; refusing to overwrite")
        source.write_bytes((copies / mutant.source).read_bytes())
        for relative, expected in pristine.items():
            restored = (ROOT / relative).read_bytes()
            assert restored == expected, (mutant.name, relative, "restored bytes differ")
        result["restored_sha256"] = sha256(source.read_bytes())
        result["restored_bytes_equal"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-root", type=Path, default=Path("/private/tmp/sf-rv-a1"))
    parser.add_argument("--only", choices=[mutant.name for mutant in MUTANTS], nargs="+")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    scratch = args.scratch_root.resolve()
    assert "sf-rv-a1" in str(scratch), "mutation scratch must belong to sf-rv-a1"
    assert args.timeout > 0
    mutations = scratch / "mutations"
    mutations.mkdir(parents=True, exist_ok=True)
    # The Rules require the OS temp root to be read; a denied default cache or
    # top-level allocation uses the already permitted job scratch instead.
    system_tmp = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
    lock = mutations / "source-mutation.lock"
    with lock.open("x") as guard:
        guard.write(str(os.getpid()) + "\n")
    try:
        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=mutations))
        selected = [mutant for mutant in MUTANTS if not args.only or mutant.name in args.only]
        pristine = {relative: (ROOT / relative).read_bytes() for relative in sorted({m.source for m in selected})}
        copies = run_dir / "pristine"
        for relative, original in pristine.items():
            copy = copies / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(original)
            assert copy.read_bytes() == original
        cache = scratch / "uv-cache"
        report = {"system_tmpdir": system_tmp, "scratch_fallback": str(scratch), "results": []}
        report_path = run_dir / "results.json"
        for mutant in selected:
            result = run_mutant(mutant, pristine, copies, run_dir, cache, args.timeout)
            report["results"].append(result)
            report_path.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(result), flush=True)
        print(f"Results: {report_path}", flush=True)
        return int(not all(result["killed"] for result in report["results"]))
    finally:
        lock.unlink()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
