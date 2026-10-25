"""Mutants of D-LANE-SCOPE1's guard edit, each to be killed by a named test.

    python tools/guard_subfleet_root_mutations.py --scratch <dir containing /sf->

Each mutant is applied to a fresh `git archive` of HEAD under the scratch
directory (never to this checkout), then its named test in
tests/unit/test_guard_subfleet_root.py runs there. A kill is a failed test
assertion: an error, a skip or a pass is not. The unmutated control must pass
every named test first. Prints one JSON row per mutant; exit 0 only when the
control passes and every mutant is killed. Exports are removed afterwards.
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

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/unit/test_guard_subfleet_root.py"
HOOK = "private/guard/never-rules-hook.sh"
TRUST = "private/guard/TRUST"
#: The original reviewed pair's commit (release/217 at abe117e67). Its TRUST still pins codex-cli 0.153.3,
#: behind the installed TRUST (0.159.0) that this revision's TRUST is built on.
BASE = "abe117e676b042b0cc8b4208dc25da52820221e2"
NEW_ROOTS = "|\\.subfleet(/[^/[:space:]'\\\"]+)?"
#: r2 (the hub's review r1, P2-3): $SUBFLEET_ROOT / $SUBFLEET_HOME as spellings of ~/.subfleet, and the
#: ${…} normalization on both `judged` lines.
ENV_ROOTS = "|\\\\\\$SUBFLEET_(ROOT|HOME)(/[^/[:space:]'\\\"]+)?"
ENV_SED = " s/\\$[{]SUBFLEET_ROOT[}]/$SUBFLEET_ROOT/g; s/\\$[{]SUBFLEET_HOME[}]/$SUBFLEET_HOME/g'"
OLD_HOOK_SHA256 = "a60d1c514d3a3bcec68c246a33c849c11fd37e1bdf60d886650cd9d6651390db"
NEW_HOOK_SHA256 = "dbe21e1d9a2ed3f57a7675fd09d3f8cacb2f73a6bfec7fd22e800da3691a041c"
ROOT_CORE = 'root_core="/|/\\\\*|/Users/?|(${broad_re})/?"'

# name -> (file, old text, new text, the named test that must kill it)
MUTATIONS = {
    "new-roots-removed": (HOOK, NEW_ROOTS, "", "test_review_inputs_are_denied"),
    "child-form-dropped": (HOOK, NEW_ROOTS, "|\\.subfleet", "test_each_direct_child_is_a_root"),
    "prefix-match": (HOOK, NEW_ROOTS, "|\\.subfleet(/[^[:space:]'\\\"]+)?",
                     "test_two_or_more_levels_under_subfleet_stay_scoped"),
    # The roots moved to their own root_core alternative, without the shared `/?`.
    "trailing-slash-missed": (HOOK, None, None, "test_trailing_slash_spellings_are_denied"),
    "trust-hash-stale": (TRUST, None, None, "test_trust_pins_the_new_hook_bytes"),
    # The hub review's finding: a TRUST built on the repository's stale copy rolls the lane pin back.
    "trust-built-on-repo-copy": (TRUST, None, None, "test_trust_keeps_the_installed_lane_pin"),
    # r2: the env spellings dropped (their broad_re alternative and their ${…} normalization).
    "env-spellings-dropped": (HOOK, None, None, "test_env_spellings_of_subfleet_are_denied"),
}
FENCE = "test_only_the_roots_the_header_and_the_message_changed"


def export(destination: Path) -> None:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "HEAD"],
                             check=True, capture_output=True).stdout
    destination.mkdir(parents=True)
    with tempfile.TemporaryFile() as handle:
        handle.write(archive)
        handle.seek(0)
        with tarfile.open(fileobj=handle) as tar:
            tar.extractall(destination, filter="tar")


def mutate(tree: Path, name: str) -> None:
    file, old, new, _ = MUTATIONS[name]
    path = tree / file
    if name == "trust-hash-stale":                 # the new TRUST, its hook_sha256 left at the old bytes'
        text = path.read_text(encoding="utf-8")
        assert text.count(NEW_HOOK_SHA256) == 1
        path.write_text(text.replace(NEW_HOOK_SHA256, OLD_HOOK_SHA256), encoding="utf-8")
        return
    if name == "trust-built-on-repo-copy":         # the repository's copy at BASE, new hook hash, new sentence
        base = json.loads(subprocess.run(["git", "-C", str(ROOT), "show", f"{BASE}:{TRUST}"],
                                         check=True, capture_output=True, text=True).stdout)
        new = json.loads(path.read_text(encoding="utf-8"))
        base["hook_sha256"] = new["hook_sha256"]
        base["provenance"] += new["provenance"][new["provenance"].index(" 2026-10-24, D-LANE-SCOPE1:"):]
        path.write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")
        return
    text = path.read_text(encoding="utf-8")
    if name == "env-spellings-dropped":
        assert text.count(ENV_ROOTS) == 1 and text.count(ENV_SED) == 2, (text.count(ENV_ROOTS), text.count(ENV_SED))
        text = text.replace(ENV_ROOTS, "").replace(ENV_SED, "'").replace("/$HOME/g;'", "/$HOME/g'")
    elif name == "trailing-slash-missed":
        assert text.count(NEW_ROOTS) == 1 and text.count(ROOT_CORE) == 1
        text = text.replace(NEW_ROOTS, "").replace(
            ROOT_CORE, ROOT_CORE[:-1] + "|(/Users/[^/[:space:]'\\\"]+|~|\\\\\\$HOME)/\\.subfleet"
                                        "(/[^/[:space:]'\\\"]+)?\"")
    else:
        assert text.count(old) == 1, (name, old)
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def pytest(tree: Path, tests: list[str], scratch: Path) -> dict[str, str]:
    """{test name: passed|failed|error|skipped} from one pytest run in ``tree``."""
    report = scratch / f"{tree.name}.xml"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["TMPDIR"] = str(scratch)
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}",
                    *[f"{TEST}::{test}" for test in tests]], cwd=tree, env=env, capture_output=True)
    import xml.etree.ElementTree as ElementTree
    outcomes = {}
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        kinds = {child.tag for child in case}
        outcomes[case.get("name")] = ("failed" if "failure" in kinds else "error" if "error" in kinds
                                      else "skipped" if "skipped" in kinds else "passed")
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--only", nargs="*", default=list(MUTATIONS))
    args = parser.parse_args()
    if "/sf-" not in str(args.scratch.resolve()):
        parser.error("--scratch must be a /sf- scratch directory")
    scratch = Path(tempfile.mkdtemp(prefix="sf-mutants-", dir=args.scratch))
    ok = True
    try:
        named = sorted({MUTATIONS[name][3] for name in args.only} | {FENCE})
        control = scratch / "control"
        export(control)
        outcome = pytest(control, named, scratch)
        print(json.dumps({"mutant": "control", "outcomes": outcome}), flush=True)
        ok = all(outcome.get(test) == "passed" for test in named)
        for name in args.only:
            tree = scratch / name
            export(tree)
            mutate(tree, name)
            test = MUTATIONS[name][3]
            outcome = pytest(tree, [test, FENCE], scratch)
            killed = outcome.get(test) == "failed"
            ok = ok and killed
            print(json.dumps({"mutant": name, "named_test": test, "killed": killed, "outcomes": outcome}),
                  flush=True)
    finally:
        if "/sf-" in str(scratch):
            shutil.rmtree(scratch)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
