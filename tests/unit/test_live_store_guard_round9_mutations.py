"""D-ST1 r9: each source mutation must fail a named round-9 test.

A fresh Python child imports the round-9 tests from a shadow copy of the source
and calls the named test function on each case, first unmutated (the control),
then with the exact mutation. Children are not pytest processes; the file runs
under the parent's single pytest/lock hold. A mutant that approves a launch runs
only `sw_vers` or `ls`; nothing here starts the reader.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from tests.unit.test_live_store_guard_round7_mutations import REPO, shadow  # noqa: F401 (fixture)

ROUND9 = "tests/unit/test_live_store_guard_round9.py"
SCRATCH = "{scratch}"  # A fresh directory per case, for a test's tmp_path.

# name: (exact original, replacement, named test, cases). The first three are
# review r9's requested mutations; the rest pin each other rule this round adds.
MUTANTS = {
    "fail-open": (
        "        # A wrapper launch whose check cannot finish is refused (C-20.6).\n        return True\n",
        "        # A wrapper launch whose check cannot finish is refused (C-20.6).\n        return False\n",
        "test_p3_1_a_check_that_cannot_finish_is_refused",
        [["relative-entry-after-deleted-cwd", SCRATCH], ["unbalanced-split-quote", SCRATCH],
         ["malformed-recorder", SCRATCH]]),
    "leading-slash-collapse": (
        'directory = "/" + directory.lstrip("/")', "directory = directory",
        "test_p3_3_double_slash_system_directory_is_refused", [["//usr/bin", "sw_vers"], ["//bin", "ls"]]),
    "grouped-options": (
        "for index, flag in enumerate(group):", "for index, flag in enumerate(group[:1]):",
        "test_p3_2_grouped_env_options_are_walked",
        [[["-vu", "PATH", "sw_vers"]], [["-vuPATH", "sw_vers"]], [["-vP", "/usr/bin", "sw_vers"]],
         [["-vS", "PATH=/usr/bin sw_vers"]], [["-v-", "sw_vers"]]]),
    "first-path-key-only": (
        "return found or [None]", "return found[:1] or [None]",
        "test_p3_1_text_and_bytes_path_keys_are_both_judged", [[]]),
    "working-directory-always": (
        "if not os.path.isabs(directory):", "if True:",
        "test_p3_1_absolute_entries_never_read_the_working_directory", [[SCRATCH]]),
    "split-escapes-emulated": (
        '_ENV_SPLIT_REFUSED = "$\\\\#"', '_ENV_SPLIT_REFUSED = ""',
        "test_p3_2_env_split_string_env_would_rewrite_is_refused",
        [["PATH=${SYS} sw_vers"], ["PATH=/usr/bin\\_sw_vers"]]),
    "split-spaces-shlex": (
        '_ENV_SPLIT_SPACES = " \\t\\n\\v\\f\\r"', '_ENV_SPLIT_SPACES = " \\t\\n\\r"',
        "test_p3_2_env_split_string_env_would_rewrite_is_refused",
        [["PATH=/usr/bin\vsw_vers"], ["PATH=/usr/bin\fsw_vers"]]),
    "altpath-only": (
        "[altpath, child]", "[altpath]",
        "test_p3_2_env_alternate_path_and_child_path_are_both_judged", [[["sw_vers"]]]),
    "chdir-ignored": (
        "base = start if chdir is None else os.path.join(start, chdir)", "base = start",
        "test_p3_2_env_chdir_is_the_base_of_relative_entries", [[]]),
    "tilde-entry": (
        '        if entry.startswith("~"):\n            raise ValueError("bash expands ~ in a PATH entry")\n', "",
        "test_p3_3_tilde_entry_is_refused", [[]]),
    "family-ksh": (
        '"dash", "ksh", "csh"', '"dash", "csh"',
        "test_p3_4_named_wrappers_with_system_path_are_refused", [["ksh", SCRATCH]]),
    "arch-child-path": (
        "return [path, _arch(argv, path)[0]], None", "return [path], None",
        "test_p3_4_arch_environment_options_are_judged",
        [[["-c"], ["sw_vers"]], [["-d", "PATH"], ["sw_vers"]], [["-e", "PATH=/usr/bin"], ["sw_vers"]]]),
}

PROBE = '''
import json, sys
from pathlib import Path
from tests.unit import test_live_store_guard_round9 as round9
name, cases, scratch = sys.argv[1], json.loads(sys.argv[2]), Path(sys.argv[3])
scratch.mkdir()
test = getattr(round9, name)
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
def test_round9_mutant_fails_its_named_test(tmp_path, shadow, mutant):
    original, replacement, name, cases = MUTANTS[mutant]
    (shadow / "tests" / "unit").mkdir(exist_ok=True)
    shutil.copyfile(REPO / ROUND9, shadow / ROUND9)
    source = shadow / "subfleet/credentials.py"
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
