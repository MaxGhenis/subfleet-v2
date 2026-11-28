"""Run the final regression on the starting daemon, restoring working bytes."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

SOURCE = Path('subfleet/daemon.py')
EVIDENCE = Path(__file__).resolve().parent
fixed = SOURCE.read_bytes()
baseline = subprocess.check_output([
    'git', 'show', '4fd796b49ae63417e845c93ad8f456978c161f1c:subfleet/daemon.py'])
try:
    SOURCE.write_bytes(baseline)
    result = subprocess.call([
        sys.executable, str(EVIDENCE / 'run.py'), 'final-regression-before',
        'tests/fake/test_resume_contract.py', '-k',
        'test_resume_keeps_source_effort_when_policy_aliases_share_a_model_id', '-q'])
finally:
    SOURCE.write_bytes(fixed)
    assert SOURCE.read_bytes() == fixed
    (EVIDENCE / 'baseline-restoration.json').write_text(json.dumps({
        'baseline_git_object': '4fd796b49ae63417e845c93ad8f456978c161f1c:subfleet/daemon.py',
        'baseline_sha256': hashlib.sha256(baseline).hexdigest(),
        'restored_sha256': hashlib.sha256(fixed).hexdigest(),
        'restoration_matches': SOURCE.read_bytes() == fixed,
    }, indent=2) + '\n')
sys.exit(result)
