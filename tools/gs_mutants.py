"""Mutants of the guard sync r2's hook edit (the two Axiom blocks and the Claude prefilter line), each to be
killed by a named test.

    python tools/gs_mutants.py --scratch <dir containing /sf->

Re-created on 2026-11-14, for the merge of release/217 into the guard sync, from the Mutants table of the r2
build report (docs/reports/2026-11-11-guard-sync.md); the build's own script was not kept. Each mutant is
applied alone to the hook in a fresh `git archive` of HEAD under the scratch directory (never to this
checkout), and SELECTION runs there: DriftTests, the two pins and the undo/rebuild checks, and both Axiom
rules' `blocked` decision cases. A kill is a failed test (a junit <failure>): an error, a skip or a pass is
not. Each mutant names the test that must kill it; every other failure is listed beside it. The unmutated
control must pass every selected test first. Prints one JSON row per run; exit 0 only when the control passes
and every mutant is killed by its named test. Exports are removed afterwards.
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
HOOK = "private/guard/never-rules-hook.sh"
BLOCKS = "tests/fixtures/guard/claude-blocks"
DOC = "docs/private-guard-overlay.md"
SYNC = "tests/unit/test_guard_sync.py"
OVERLAY = "tests/unit/test_guard_overlay.py"
BASE = "tests/unit/test_guard_subfleet_root.py"
FILES = [SYNC, OVERLAY, BASE]
SELECTION = ("DriftTests or test_private_overlay_bytes or test_gs_undoing or test_merge_undoing"
             " or test_only_the_roots or (axiom_brief_regime and blocked) or (hand_authored_rulespec and blocked)")
#: The first prefilter literal's line, which the Claude hook's prefilter line (claude-blocks/prefilter.txt) heads.
PREFILTER_HEAD = "printf '%s' \"$command\" | grep -qE 'pending_deployments|"
COMMIT_TERM = "|git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+commit"

DRIFT = f"{SYNC}::DriftTests::"
#: name -> (what it does, the named test that must kill it)
MUTATIONS = {
    "1-drop-hand-authored-rulespec": ("the [hand-authored-rulespec] block removed whole",
                                      DRIFT + "test_every_pinned_block_is_in_the_overlay_byte_for_byte"),
    "2-one-byte-in-axiom-brief-regime": ("[axiom-brief-regime]'s message: `This brief` -> `this brief`",
                                         DRIFT + "test_every_pinned_block_is_in_the_overlay_byte_for_byte"),
    "3-commit-term-out-of-the-prefilter": ("the `git([[:space:]]+-C…)?[[:space:]]+commit` term left out of the "
                                           "first prefilter literal",
                                           DRIFT + "test_the_first_prefilter_literal_is_the_claude_hooks_line"),
    "4-swap-the-two-blocks": ("[hand-authored-rulespec] before [axiom-brief-regime]",
                              DRIFT + "test_the_overlay_keeps_its_nine_rule_blocks_in_order"),
}

#: What the selection needs of HEAD.
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
    brief = (tree / BLOCKS / "axiom-brief-regime.sh").read_text(encoding="utf-8")
    hand = (tree / BLOCKS / "hand-authored-rulespec.sh").read_text(encoding="utf-8")
    if name == "1-drop-hand-authored-rulespec":
        assert text.count(hand) == 1
        text = text.replace(hand, "")
    elif name == "2-one-byte-in-axiom-brief-regime":
        old = 'block "[axiom-brief-regime] This brief'
        assert text.count(old) == 1
        text = text.replace(old, 'block "[axiom-brief-regime] this brief')
    elif name == "3-commit-term-out-of-the-prefilter":
        lines = text.split("\n")
        at = [i for i, line in enumerate(lines) if line.startswith(PREFILTER_HEAD)]
        assert len(at) == 1 and lines[at[0]].count(COMMIT_TERM) == 1
        lines[at[0]] = lines[at[0]].replace(COMMIT_TERM, "")
        text = "\n".join(lines)
    elif name == "4-swap-the-two-blocks":
        assert text.count(brief + hand) == 1
        text = text.replace(brief + hand, hand + brief)
    else:
        raise KeyError(name)
    path.write_text(text, encoding="utf-8")


def pytest(tree: Path, scratch: Path) -> dict[str, str]:
    """{file::test id: passed|failed|error|skipped} from one pytest run of SELECTION in ``tree``."""
    report = scratch / f"{tree.name}.xml"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "GUARD_DIFF_CACHE")}
    env["TMPDIR"] = str(scratch)
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}",
                    f"--basetemp={scratch / ('pytest-gsm-' + tree.name)}", "-k", SELECTION, *FILES],
                   cwd=tree, env=env, capture_output=True)
    outcomes: dict[str, str] = {}
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        kinds = {child.tag for child in case}
        outcome = ("failed" if "failure" in kinds else "error" if "error" in kinds
                   else "skipped" if "skipped" in kinds else "passed")
        parts = case.get("classname").split(".")
        module = next(i for i, part in enumerate(parts) if part.startswith("test_"))
        path = "/".join(parts[:module + 1]) + ".py"
        name = "::".join([path, *parts[module + 1:], case.get("name")])
        outcomes[name] = outcome
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--only", nargs="*", default=list(MUTATIONS))
    parser.add_argument("--no-control", action="store_true", help="skip the control (it ran in an earlier call)")
    args = parser.parse_args()
    if "/sf-" not in str(args.scratch.resolve()):
        parser.error("--scratch must be a /sf- scratch directory")
    scratch = Path(tempfile.mkdtemp(prefix="sf-mutants-", dir=args.scratch))
    ok = True
    try:
        if not args.no_control:
            control = scratch / "control"
            export(control)
            outcome = pytest(control, scratch)
            named = {MUTATIONS[name][1] for name in MUTATIONS}
            control_ok = bool(outcome) and all(value == "passed" for value in outcome.values()) \
                and named <= set(outcome)
            ok = control_ok
            print(json.dumps({"mutant": "control", "passed": control_ok, "tests": len(outcome),
                              "not_passed": sorted(k for k, v in outcome.items() if v != "passed")}), flush=True)
        for name in args.only:
            tree = scratch / name
            export(tree)
            original = (tree / HOOK).read_bytes()
            mutate(tree, name)
            assert (tree / HOOK).read_bytes() != original
            outcome = pytest(tree, scratch)
            test = MUTATIONS[name][1]
            killed = outcome.get(test) == "failed"
            ok = ok and killed
            failed = sorted(k for k, v in outcome.items() if v == "failed")
            print(json.dumps({"mutant": name, "what": MUTATIONS[name][0], "named_test": test, "killed": killed,
                              "failed": len(failed), "passed": sum(v == "passed" for v in outcome.values()),
                              "errors": sorted(k for k, v in outcome.items() if v == "error"),
                              "killed_by": failed}), flush=True)
    finally:
        if "/sf-" in str(scratch):
            shutil.rmtree(scratch)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
