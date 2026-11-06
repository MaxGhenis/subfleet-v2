"""Mutants of D-SEARCH-REL1's hook edit (the embedded parser), each to be killed by named tests.

    python tools/guard_search_rel_mutations.py --scratch <dir containing /sf->

Each mutant is applied to a fresh `git archive` of HEAD under the scratch
directory (never to this checkout), then its named tests run there. A kill is a
failed test (an assertion or an exception in the test's body): an error in a
fixture, a skip or a pass is not. The unmutated control must pass every named
test first. Prints one JSON row per mutant, with each named test's outcome;
exit 0 only when the control passes and every mutant is killed. Exports are
removed afterwards.

The mutants (a)-(f) are the brief's; (g) is this change's own (the loader's alarm).
(c) and (e) edit the regex judgment's last statement, outside the parser block,
so the r12d rebuild (which undoes only this change's text) no longer hashes to
R12D_HOOK_SHA256: their named tests are ones that do not rebuild r12d.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ElementTree

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/unit/test_guard_search_rel.py"
OVERLAY = "tests/unit/test_guard_overlay.py"
BASE = "tests/unit/test_guard_subfleet_root.py"
HOOK = "private/guard/never-rules-hook.sh"
DOC = "docs/private-guard-overlay.md"

ASK = 'if [ -n "${us_ask-}" ]; then\n  us_boot='
HIT = '  if [ -n "$hit" ]; then\n    [ -n "$matched" ]'
#: The interpreter the hook takes when the test seam names one (every named test names one).
CAN_RUN = '[ -x "${CODEX_GUARD_TEST_PYTHON:-/opt/homebrew/bin/python3}" ]'

ROWS_TEST = f"{TEST}::test_r1_the_hook_gives_each_row_the_parser_verdict"
STARTS_TEST = f"{TEST}::test_r4_the_interpreter_starts_only_when_a_searcher_is_named_and_the_regex_passed"
EXIT_TEST = f"{TEST}::test_fallback_a_nonzero_exit_is_no_answer"
NONE_TEST = f"{TEST}::test_fallback_an_interpreter_that_cannot_run_is_none"
BYTES_TEST = f"{TEST}::test_r5_the_heredoc_holds_the_hub_parser_byte_for_byte"
ARGS_TEST = f"{TEST}::test_r5_the_interpreter_gets_the_bytes_the_command_and_the_arguments"
ALARM_TEST = f"{TEST}::test_fallback_the_loader_arms_a_5_s_alarm"
LATE_TEST = f"{TEST}::test_fallback_no_answer_within_5_s_is_no_answer"
OVERLAY_PIN_TEST = f"{OVERLAY}::test_private_overlay_bytes_remain_the_original_reviewed_pair"
BASE_PIN_TEST = f"{BASE}::test_trust_pins_the_new_hook_bytes"

# name -> (edits to HOOK as (old, new), each old occurring once; the named tests)
MUTATIONS = {
    # (a) the hook never calls the parser.
    "a-parser-never-called": ([(ASK, 'if [ -n "" ]; then\n  us_boot=')], [ROWS_TEST, STARTS_TEST, EXIT_TEST]),
    # (b) the parser runs, and its answer is ignored.
    "b-answer-ignored": ([('&& [ -n "$us_hit" ]; then', '&& [ -n "" ]; then')], [ROWS_TEST, EXIT_TEST]),
    # (c) the regex judgment's block is skipped whenever the parser can run (a searcher named, an
    # interpreter there).
    "c-regex-skipped-when-the-parser-runs": (
        [(HIT, f'  if [ -n "$hit" ] && ! {{ [ -n "${{us_ask-}}" ] && {CAN_RUN}; }}; then\n    [ -n "$matched" ]')],
        [STARTS_TEST, ROWS_TEST]),
    # (d) the parser runs on every command that reaches the block, searcher or not.
    "d-parser-on-every-command": ([(ASK, 'if :; then\n  us_boot=')], [STARTS_TEST]),
    # (e) no fallback: with no interpreter the regex judgment never blocks, so nothing blocks.
    "e-no-fallback": ([(HIT, f'  if [ -n "$hit" ] && {CAN_RUN}; then\n    [ -n "$matched" ]')], [NONE_TEST]),
    # (f) one byte of the embedded parser changed, in its docstring (z -> s), so its behaviour stays.
    "f-one-parser-byte": ([('"""Argument-aware analyzer for the [unscoped-search] rule',
                            '"""Argument-aware analyser for the [unscoped-search] rule')],
                          [BYTES_TEST, ARGS_TEST, OVERLAY_PIN_TEST, BASE_PIN_TEST]),
    # (g) the loader arms no alarm.
    "g-no-alarm": ([("signal.alarm(5)\n", "")], [ALARM_TEST, LATE_TEST]),
}

#: What the named tests need of HEAD.
EXPORTED = ["subfleet", "tests", "tools", "private", "bin", "pyproject.toml", DOC]


def export(destination: Path) -> None:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "HEAD", *EXPORTED],
                             check=True, capture_output=True).stdout
    destination.mkdir(parents=True)
    with tempfile.TemporaryFile() as handle:
        handle.write(archive)
        handle.seek(0)
        with tarfile.open(fileobj=handle) as tar:
            tar.extractall(destination, filter="tar")


def mutate(tree: Path, name: str) -> None:
    path = tree / HOOK
    text = path.read_text(encoding="utf-8")
    for old, new in MUTATIONS[name][0]:
        assert text.count(old) == 1, (name, old[:60], text.count(old))
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def pytest(tree: Path, tests: list[str], scratch: Path, first_kill: bool = False) -> dict[str, str]:
    """{file::test name: passed|failed|error|skipped} from one pytest run in ``tree``; a parametrized
    test's cases fold into its name (failed if any failed). With ``first_kill`` the run stops at its first
    failure (pytest -x), so the named tests after the killer have no outcome."""
    report = scratch / f"{tree.name}.xml"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and k != "GUARD_DIFF_CACHE"}
    env["TMPDIR"] = str(scratch)
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}",
                    f"--basetemp={scratch / ('bt-' + tree.name)}", *(["-x"] if first_kill else []), *tests],
                   cwd=tree, env=env, capture_output=True)
    outcomes: dict[str, str] = {}
    rank = ["passed", "skipped", "error", "failed"]
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        kinds = {child.tag for child in case}
        outcome = ("failed" if "failure" in kinds else "error" if "error" in kinds
                   else "skipped" if "skipped" in kinds else "passed")
        module = case.get("classname").rsplit(".", 1)[-1]
        name = f"tests/unit/{module}.py::{case.get('name').split('[', 1)[0]}"
        outcomes[name] = max(outcomes.get(name, "passed"), outcome, key=rank.index)
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--only", nargs="*", default=list(MUTATIONS))
    parser.add_argument("--no-control", action="store_true", help="skip the control (it ran in an earlier call)")
    parser.add_argument("--first-kill", action="store_true",
                        help="stop each mutant's run at its first failed test (pytest -x); the control runs whole")
    args = parser.parse_args()
    if "/sf-" not in str(args.scratch.resolve()):
        parser.error("--scratch must be a /sf- scratch directory")
    scratch = Path(tempfile.mkdtemp(prefix="sf-mutants-", dir=args.scratch))
    ok = True
    try:
        if not args.no_control:
            named = sorted({test for name in args.only for test in MUTATIONS[name][1]})
            control = scratch / "control"
            export(control)
            outcome = pytest(control, named, scratch)
            print(json.dumps({"mutant": "control", "outcomes": outcome}), flush=True)
            ok = all(outcome.get(test) == "passed" for test in named)
        for name in args.only:
            tree = scratch / name
            export(tree)
            mutate(tree, name)
            tests = MUTATIONS[name][1]
            outcome = pytest(tree, tests, scratch, first_kill=args.first_kill)
            killed_by = [test for test in tests if outcome.get(test) == "failed"]
            ok = ok and bool(killed_by)
            print(json.dumps({"mutant": name, "killed": bool(killed_by), "killed_by": killed_by,
                              "outcomes": outcome}), flush=True)
    finally:
        if "/sf-" in str(scratch):
            shutil.rmtree(scratch)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
