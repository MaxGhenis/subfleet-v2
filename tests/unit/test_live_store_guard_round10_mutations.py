"""D-ST1 r10: each source mutation must fail a named round-10 test.

A fresh Python child imports the round-10 tests from a shadow copy of the
source and calls the named test function on each case, first unmutated (the
control), then with the exact mutation. Children are not pytest processes; the
file runs under the parent's single pytest/lock hold. A mutant that approves a
launch runs only `sw_vers`, `true` or a registered recording stand-in; nothing
here starts the reader.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.unit.test_live_store_guard_round7_mutations import REPO, shadow  # noqa: F401 (fixture)

ROUND10 = "tests/unit/test_live_store_guard_round10.py"
CREDENTIALS = "subfleet/credentials.py"
NET = "tests/keychain_standin.py"
SCRATCH = "{scratch}"  # A fresh directory per case, for a test's tmp_path.

# name: (file, exact original, replacement, named test, cases). The first three
# are the review's requested mutations; the rest pin each other rule this round adds.
MUTANTS = {
    "unconditional-search": (
        CREDENTIALS, "None if os.path.dirname(name) else os.get_exec_path(env)", "os.get_exec_path(env)",
        "test_p2_a_program_by_path_with_both_path_keys_stays_approved", [["text-first"], ["bytes-first"]]),
    "no-command-word": (
        CREDENTIALS,
        "            if os.path.dirname(word) and _matches(os.path.join(base, word), reader):\n"
        "                return True\n", "",
        "test_p3_a_wrapper_running_the_reader_by_path_is_refused",
        [["nice", SCRATCH], ["env", SCRATCH], ["taskpolicy -c utility", SCRATCH], ["script", SCRATCH]]),
    "no-nested-judgement": (
        CREDENTIALS, "return inner in _family and reaches(inner, command, path, shell, base, depth + 1)",
        "return False",
        "test_p3_nested_wrappers_are_judged_with_the_environment_the_outer_leaves",
        [["nice env -i"], ["env /usr/bin/env -i"], ["taskpolicy -c utility env -i"], ["arch -arm64 env -i"],
         ["nice env PATH=/usr/bin"]]),
    "direct-fail-open": (
        CREDENTIALS, "            refused = True\n", "            return\n",
        "test_p2_a_bare_name_whose_search_cannot_finish_is_refused", [[SCRATCH]]),
    "depth-unbounded": (
        CREDENTIALS, "if depth > _depth:", "if False:", "test_p3_nesting_is_judged_to_a_fixed_depth", [[]]),
    "family-stdbuf": (
        CREDENTIALS, '    "stdbuf",\n})', "})", "test_p3_stdbuf_joins_the_checked_wrappers", [[]]),
    "script-shell": (
        CREDENTIALS, 'if name == "script" and not command and shell != "":', "if False:",
        "test_p3_script_without_a_command_is_judged_by_its_shell", [[SCRATCH]]),
    "env-shell": (
        CREDENTIALS, 'return command, child, _env(argv, shell, variable="SHELL")[0]', "return command, child, shell",
        "test_p3_script_shell_set_by_an_outer_wrapper_is_judged", [[SCRATCH]]),
    "nice-obsolete-number": (
        CREDENTIALS, '        args[1] = "-n" + args[1][1:]\n', "        pass\n",
        "test_p3_wrappers_running_ordinary_commands_stay_approved", [[1, SCRATCH]]),
    "getopt-unknown-option": (
        CREDENTIALS, 'if flag == ":" or flag not in options:\n                raise ValueError(f"no option -{flag}")',
        'if flag == ":" or flag not in options:\n                break',
        "test_p3_a_wrapper_option_its_getopt_rejects_is_refused",
        [[["/usr/bin/nice", "-x", "/usr/bin/true"]], [["/usr/bin/nohup", "-n", "1", "/usr/bin/true"]]]),
    "net-unconditional-search": (
        NET, "    if not os.path.dirname(name):\n        # A launch searches PATH only for a bare name",
        "    if True:\n        # A launch searches PATH only for a bare name",
        "test_p2_safety_net_judges_a_reader_path_with_both_path_keys", [["text-first"], ["bytes-first"]]),
    "net-fail-open": (
        NET, 'return f"{event}: the check cannot finish ({type(error).__name__})"', "return None",
        "test_p2_safety_net_refuses_a_check_that_cannot_finish", [[]]),
}

PROBE = '''
import json, sys
from pathlib import Path
from tests.unit import test_live_store_guard_round10 as round10
name, cases, scratch = sys.argv[1], json.loads(sys.argv[2]), Path(sys.argv[3])
scratch.mkdir()
test = getattr(round10, name)
failed = []
for index, case in enumerate(cases):
    here = scratch / f"case-{index}"
    here.mkdir()
    args = [here if arg == "{scratch}" else arg for arg in case]
    try:
        test(*args)
    except BaseException as error:
        failed.append(f"{case!r}: {type(error).__name__}: {str(error)[:200]}")
if failed:
    print("NAMED TEST FAILED: " + name, *failed, sep="\\n")
    sys.exit(1)
print("named test passed:", name, len(cases), "cases")
'''


@pytest.mark.parametrize("mutant", MUTANTS)
def test_round10_mutant_fails_its_named_test(tmp_path, shadow, mutant):
    relative, original, replacement, name, cases = MUTANTS[mutant]
    (shadow / "tests" / "unit").mkdir(exist_ok=True)
    shutil.copyfile(REPO / ROUND10, shadow / ROUND10)
    source = shadow / relative
    saved = source.read_text()
    assert saved.count(original) == 1, (mutant, "stale mutation")
    env = {**os.environ, "PYTHONPATH": str(shadow), "PYTHONDONTWRITEBYTECODE": "1"}

    def run(label):
        return subprocess.run([sys.executable, "-c", PROBE, name, json.dumps(cases), str(tmp_path / label)],
                              cwd=shadow, env=env, capture_output=True, text=True, timeout=300)
    control = run("control")
    assert control.returncode == 0, control.stdout + control.stderr
    try:
        source.write_text(saved.replace(original, replacement))
        mutated = run("mutant")
    finally:
        source.write_text(saved)
    assert mutated.returncode != 0, f"survived: {mutant}\n{mutated.stdout}"
    assert "NAMED TEST FAILED: " + name in mutated.stdout, mutated.stdout + mutated.stderr
