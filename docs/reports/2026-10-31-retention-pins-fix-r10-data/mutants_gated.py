"""Named mutants only; queue while the host is healthy, with every child waited."""
import datetime
import json
import os
from pathlib import Path
import sys
import time
from batch import call_waited

DATA = Path(__file__).resolve().parent
cutoff = datetime.datetime.fromisoformat('2026-10-31T15:33:45+00:00')
plan = json.loads((DATA / (sys.argv[1] if len(sys.argv) > 1 else 'mutants-plan.json')).read_text())
for name, args in plan:
    while os.getloadavg()[0] > 100 and not (DATA / 'mutants-primary-only').exists():
        if datetime.datetime.now(datetime.UTC) >= cutoff:
            break
        (DATA / 'mutant-gate.json').write_text(json.dumps(dict(name=name,
            waiting_since_or_at=datetime.datetime.now(datetime.UTC).isoformat(),
            load=os.getloadavg(), threshold=100), indent=2))
        time.sleep(5)
    if datetime.datetime.now(datetime.UTC) >= cutoff:
        (DATA / (name + '-unrun.json')).write_text(json.dumps(dict(args=args,
            reason='Four-hour hold-start cutoff'), indent=2))
        continue
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    if (DATA / 'mutants-primary-only').exists():
        # After all ordinary holds finish, use only the original shared lock.
        env['SUITE_HOLD_SLOT_B'] = '/private/tmp/claude-501/subfleet-suites.lock'
    call_waited([sys.executable, str(DATA / 'run.py'), name, *args], env=env)
