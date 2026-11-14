from pathlib import Path
import subprocess
import sys
root = Path('/private/tmp/sf-wc-b1')
files = (root / 'regression-files.txt').read_text().splitlines()
unsupported = [
 'tests/process/test_provider_shell_environment.py::test_codex_exec_and_resume_commands_see_neither_dummy',
 'tests/process/test_provider_shell_environment.py::test_codex_conversation_turn_commands_see_neither_dummy',
 'tests/unit/test_publish_rule_round7.py::test_each_spelling_is_refused_in_each_providers_real_shell',
 'tests/unit/test_publish_rule_round7.py::test_real_codex_sandbox_keeps_the_git_denial_under_inherit_core',
]
command = [sys.executable, str(root / 'run.py'), 'affected-n4-final', 'pytest', '-q', '-n', '4', '-m', 'not real_lsof and not real_cwd', *files]
for node in unsupported:
    command += ['--deselect', node]
raise SystemExit(subprocess.call(command))
