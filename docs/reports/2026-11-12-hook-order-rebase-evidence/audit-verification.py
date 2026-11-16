"""Read-only verification of the #86 rebase onto release/217."""
from pathlib import Path
import subprocess

RELEASE = "b51f08f961e1b1dcfb93347eb080f9ddd9a9260d"
PR = "62103194a836"
CONTRACT = "docs/acceptance-contract.md"


def git_bytes(revision: str, path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{revision}:{path}"])


def changes_log(text: str) -> str:
    return text.split("## Changes in version 3\n", 1)[1].split("\n## ", 1)[0]


def clause(text: str, number: int) -> str:
    return next(line for line in text.splitlines()
                if line.startswith(f"- **C-23.{number}**"))


release_contract = git_bytes(RELEASE, CONTRACT).decode()
pr_contract = git_bytes(PR, CONTRACT).decode()
work_contract = Path(CONTRACT).read_text()
release_log = changes_log(release_contract)
work_log = changes_log(work_contract)
first_entry = next(line for line in work_log.splitlines() if line.startswith("- "))
pr_first_entry = next(line for line in changes_log(pr_contract).splitlines()
                      if line.startswith("- "))
expected_first_entry = pr_first_entry.replace(
    "a false doctor FAIL on 2026-09-28)",
    "a false doctor FAIL on 2026-09-28; rebased 2026-11-12, PR #86)",
)
assert first_entry == expected_first_entry
assert work_log.replace(first_entry + "\n", "", 1) == release_log
release_entry_count = sum(line.startswith("- ") for line in release_log.splitlines())
print(f"PASS: all {release_entry_count} release Changes-v3 entries and whitespace "
      "preserved exactly, with requested PR entry first")
assert clause(work_contract, 25) == clause(pr_contract, 25)
print("PASS: C-23.25 equals original PR verbatim")
assert clause(work_contract, 26) == clause(release_contract, 26)
print("PASS: C-23.26 equals release verbatim")

for path, revision in (
    ("tests/unit/test_hooks_install_equality.py", PR),
    ("tests/unit/test_hooks.py", RELEASE),
    ("tests/unit/test_doctor.py", RELEASE),
    ("tests/unit/test_hooks_native_guard.py", RELEASE),
    ("docs/invariants.json", RELEASE),
):
    assert Path(path).read_bytes() == git_bytes(revision, path), path
    print(f"PASS: {path} byte-identical to {revision}")

release_hooks_lines = git_bytes(RELEASE, "subfleet/hooks.py").decode().splitlines()
release_loop = release_hooks_lines[910:918]
differential_lines = Path("tests/unit/test_hooks_install_differential.py").read_text().splitlines()
loop_start = differential_lines.index(release_loop[0])
frozen_loop = differential_lines[loop_start:loop_start + len(release_loop)]
assert frozen_loop == release_loop
print("PASS: frozen differential loop exactly equals release subfleet/hooks.py:911-918")
