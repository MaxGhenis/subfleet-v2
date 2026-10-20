"""C-20.6 (D-ST3): each source mutation must fail a named test of test_live_store_guard_st3.py.

A fresh Python child imports the D-ST3 tests from a shadow copy of the source
and calls the named test on each case, first unmutated (the control), then
with the exact mutation. The child loads pytest, because the tests import it,
but is not a pytest session: it inherits this session's live markers, and each
named test replaces os.environ itself under the same spies, so a mutant's
fallback is refused before its first access. The file runs under the parent's
single pytest/lock hold.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.live_store_trap import PERSONAL
from tests.unit.test_live_store_guard_round7_mutations import REPO, shadow  # noqa: F401 (fixture)

ST3 = "tests/unit/test_live_store_guard_st3.py"
GUARD = "tests/unit/test_live_store_guard.py"
HOMES = "subfleet/home_paths.py"
COMPAT = "subfleet/compat.py"
PRUNE = "subfleet/prune_decisions.py"
EMPTY = [{"environ": {}}, {"environ": {"PATH": "/usr/bin:/bin"}}]
SCRATCH = "{scratch}"  # A fresh directory per case, for a test's tmp_path.

# name: (file, exact original, replacement, named test, cases). The first is the
# brief's mutation, the process rule removed; the rest pin each other rule D-ST3 adds.
MUTANTS = {
    "no-process-rule": (
        HOMES, 'return "_pytest" in sys.modules or "pytest" in sys.modules', "return False",
        "test_daemon_start_refuses_the_live_root_before_any_access", EMPTY),
    "process-rule-not-folded": (
        HOMES, "return pytest_process() or _session_environment(os.environ)",
        "return _session_environment(os.environ)", "test_production_state_root_refuses", EMPTY),
    "seam-ignores-home": (
        HOMES, "        return is_account_home(Path.home())\n", "        return False\n",
        "test_the_seam_keeps_the_live_root_refused_under_the_accounts_home", [{}]),
    "no-tilde-refusal": (
        HOMES, "if testing and _names_account_home(override):", "if False:",
        "test_a_tilde_override_naming_the_accounts_home_is_refused",
        [{"variable": "SUBFLEET_HOME", "home": "unset"}, {"variable": "SUBFLEET_CLAUDE_DIR", "home": "account"}]),
    "no-resolved-spelling": (
        HOMES, " or _same_directory(Path(os.path.realpath(home)), personal)", "",
        "test_a_home_spelled_through_a_link_past_a_missing_component_is_the_accounts",
        [{"tmp_path": SCRATCH}]),
    "no-lexical-spelling": (
        HOMES, " or Path(os.path.abspath(home)) == personal", "",
        "test_production_state_root_refuses_every_spelling_of_the_accounts_home",
        [{"spelling": f"{PERSONAL}/.subfleet/.."}]),
    "v1-default-unguarded": (
        COMPAT, "home_path(V1_BIN_ENV, lambda: V1_BIN_DEFAULT, env=env, test_only=True)", "V1_BIN_DEFAULT",
        "test_the_v1_install_default_is_refused", EMPTY),
    "prune-default-unguarded": (
        PRUNE, 'args.state_root = home_path("SUBFLEET_HOME", lambda: "~/.subfleet")',
        'args.state_root = os.environ.get("SUBFLEET_HOME") or "~/.subfleet"',
        "test_prune_decisions_state_root_default_is_refused", EMPTY),
}

PROBE = '''
import inspect, json, sys
from pathlib import Path
import pytest
from tests.conftest import waive_process_rule
from tests.unit import test_live_store_guard_st3 as st3
name, cases, scratch = sys.argv[1], json.loads(sys.argv[2]), Path(sys.argv[3])
scratch.mkdir()
test = getattr(st3, name)
failed = []
for index, case in enumerate(cases):
    here = scratch / f"case-{index}"
    here.mkdir()
    case = {key: here if value == "{scratch}" else value for key, value in case.items()}
    with pytest.MonkeyPatch.context() as patch:
        try:
            with st3.sealing(patch) as seal:
                if "production_process" in inspect.signature(test).parameters:
                    waive_process_rule(patch)
                    case = {**case, "production_process": None}
                test(sealed=seal, monkeypatch=patch, **case)
        except BaseException as error:
            failed.append(f"{case!r}: {type(error).__name__}: {str(error)[:300]}")
if failed:
    print("NAMED TEST FAILED: " + name, *failed, sep="\\n")
    sys.exit(1)
print("named test passed:", name, len(cases), "cases")
'''


@pytest.mark.parametrize("mutant", MUTANTS)
def test_st3_mutant_fails_its_named_test(tmp_path, shadow, mutant):
    """C-20.6 (D-ST3): the control passes the named test and the mutant fails it."""
    relative, original, replacement, name, cases = MUTANTS[mutant]
    (shadow / "tests" / "unit").mkdir(exist_ok=True)
    for test_file in (ST3, GUARD):
        shutil.copyfile(REPO / test_file, shadow / test_file)
    source = shadow / relative
    saved = source.read_text()
    assert saved.count(original) == 1, (mutant, "stale mutation")
    env = {**os.environ, "PYTHONPATH": str(shadow), "PYTHONDONTWRITEBYTECODE": "1"}

    def run(label):
        return subprocess.run([sys.executable, "-c", PROBE, name, json.dumps(cases), str(tmp_path / label)],
                              cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    control = run("control")
    assert control.returncode == 0, control.stdout + control.stderr
    try:
        source.write_text(saved.replace(original, replacement))
        mutated = run("mutant")
    finally:
        source.write_text(saved)
    assert mutated.returncode != 0, f"{mutant} survived: {mutated.stdout}"
    assert "NAMED TEST FAILED: " + name in mutated.stdout, mutated.stdout + mutated.stderr
