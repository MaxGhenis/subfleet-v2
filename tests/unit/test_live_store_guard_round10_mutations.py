"""D-ST1 r10: each source mutation must fail a named round-10 test.

A fresh Python child imports the round-10 tests from a shadow copy of the
source and calls the named test function on each case, first unmutated (the
control), then with the exact mutation. Each typed-word mutant fails its
fixed-example named test; two of them must also fail the round-10 property,
whose launches are synthetic audit events. Children
are not pytest processes; the file runs under the parent's single pytest/lock
hold. A mutant that approves a launch runs only `sw_vers`, `true` or a
registered recording stand-in; nothing here starts the reader.
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
# The lines the typed-word mutants change: argv decoding in the wrapper check,
# and the program decoding in the launch search.
ARGV = "        argv = [os.fsdecode(arg) for arg in argv]\n"
FIND = '    name = os.fsdecode(name)\n    base = "" if cwd is None else os.fsdecode(cwd)\n'

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
    "inactive-checks": (
        CREDENTIALS, "if event not in _events or not _active(pins):", "if event not in _events:",
        "test_p2_an_inactive_guard_judges_nothing", [[]]),
    "net-unconditional-search": (
        NET, "    if not os.path.dirname(name):\n        # A launch searches PATH only for a bare name",
        "    if True:\n        # A launch searches PATH only for a bare name",
        "test_p2_safety_net_judges_a_reader_path_with_both_path_keys", [["text-first"], ["bytes-first"]]),
    "net-fail-open": (
        NET, 'return f"{event}: the check cannot finish ({type(error).__name__})"', "return None",
        "test_p2_safety_net_refuses_a_check_that_cannot_finish", [[]]),
    # #245 review r1's P3: the typed-word mutants, each pinned to a fixed example,
    # and all but the blanked-word one to the property below too, whose catch
    # depends on its seed.
    "wrapper-drops-typed-words": (
        CREDENTIALS, ARGV, "        argv = [arg for arg in argv if isinstance(arg, str)]\n",
        "test_r1_p3_a_wrapper_command_given_as_bytes_or_a_path_is_judged", [["bytes"], ["path"]]),
    "wrapper-blanks-untyped-words": (
        CREDENTIALS, ARGV, '        argv = [os.fsdecode(arg) if isinstance(arg, (str, bytes, os.PathLike)) else ""'
                           " for arg in argv]\n",
        "test_r1_p3_a_wrapper_argv_word_that_names_nothing_is_refused", [[]]),
    "typed-program-names-nothing": (
        CREDENTIALS, FIND,
        '    name = name if isinstance(name, str) else ""\n    base = "" if cwd is None else os.fsdecode(cwd)\n',
        "test_r1_p3_a_program_given_as_bytes_or_a_path_is_judged", [["bytes"], ["path"]]),
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
    """C-20.6 each mutation of the guard or its safety net fails the round-10 test named for it."""
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


# #226 review r2's P3: mutants only the property's typed inputs reach. Each
# mishandles an argv word or program given as bytes, a path object or neither;
# the property at 473fe2941, whose words were all text, passed on each. The
# property must still catch them; MUTANTS above pins each to a fixed example too.
# Left out here: wrapper-blanks-untyped-words, which the property caught under
# only 9 of 30 seeds in #245 review r1's sweep, so its pin would hold by the
# seed's luck. Its fixed example is the only pin it needs.
PROPERTY_MUTANTS = {name: MUTANTS[name][1:3]
                    for name in ("wrapper-drops-typed-words", "typed-program-names-nothing")}

PROPERTY_PROBE = '''
import sys
from pathlib import Path
from tests.unit import test_live_store_guard_round10 as round10
from tests.keychain_standin import StandIn
scratch = Path(sys.argv[1])
scratch.mkdir()
found = StandIn(scratch / "guarded")
(found.path.parent / round10.ALIAS).symlink_to(found.path)
try:
    round10.test_property_no_launch_that_could_reach_the_reader_and_no_unfinished_check_is_approved(
        property_standin=found)
except BaseException as error:
    print("PROPERTY FAILED:", type(error).__name__, " ".join(str(error).split())[:300])
    sys.exit(1)
print("property passed")
'''


@pytest.mark.parametrize("mutant", PROPERTY_MUTANTS)
def test_round10_property_fails_on_each_typed_word_mutant(tmp_path, shadow, mutant):
    """C-20.6 the property catches a guard that mishandles argv words or programs given as bytes or paths."""
    original, replacement = PROPERTY_MUTANTS[mutant]
    (shadow / "tests" / "unit").mkdir(exist_ok=True)
    shutil.copyfile(REPO / ROUND10, shadow / ROUND10)
    source = shadow / CREDENTIALS
    saved = source.read_text()
    assert saved.count(original) == 1, (mutant, "stale mutation")
    env = {**os.environ, "PYTHONPATH": str(shadow), "PYTHONDONTWRITEBYTECODE": "1"}

    def run(label):
        return subprocess.run([sys.executable, "-c", PROPERTY_PROBE, str(tmp_path / label)],
                              cwd=shadow, env=env, capture_output=True, text=True, timeout=300)
    control = run("control")
    assert control.returncode == 0, control.stdout + control.stderr
    try:
        source.write_text(saved.replace(original, replacement))
        mutated = run("mutant")
    finally:
        source.write_text(saved)
    assert mutated.returncode != 0, f"survived: {mutant}\n{mutated.stdout}"
    assert "PROPERTY FAILED: " in mutated.stdout, mutated.stdout + mutated.stderr
