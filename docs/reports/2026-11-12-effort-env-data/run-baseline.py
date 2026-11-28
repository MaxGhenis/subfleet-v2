"""Check residual unrelated failures against the exact starting adapter/turn bytes."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

DATA = Path(__file__).resolve().parent
REPO = DATA.parents[2]
paths = [REPO / 'subfleet/adapters/claude.py', REPO / 'subfleet/conversations/claude_turn.py']
pristine = {path: path.read_bytes() for path in paths}
base = 'b51f08f961e1b1dcfb93347eb080f9ddd9a9260d'
try:
    for path in paths:
        path.write_bytes(subprocess.check_output(['git', 'show', f'{base}:{path.relative_to(REPO)}'], cwd=REPO))
    done = subprocess.run([sys.executable, str(DATA / 'run-tests.py'), 'baseline-residual', '-n', '4', '-q', '--tb=short',
                           'tests/fake/test_canonical_identity.py::test_two_legacy_alias_holders_oldest_exports_and_releases',
                           'tests/process/test_provider_shell_environment.py::test_codex_exec_and_resume_commands_see_neither_dummy',
                           'tests/process/test_provider_shell_environment.py::test_codex_conversation_turn_commands_see_neither_dummy'], cwd=REPO)
finally:
    for path, content in pristine.items():
        path.write_bytes(content)
        assert path.read_bytes() == content
    (DATA / 'baseline-restored.json').write_text(json.dumps({str(path.relative_to(REPO)): hashlib.sha256(content).hexdigest()
                                                           for path, content in pristine.items()}, indent=2)+'\n')
print(f'Baseline diagnostic exit {done.returncode}; all production bytes restored', flush=True)
