"""Foreground sequential holds only; every subprocess is waited to completion."""
import datetime
import json
from pathlib import Path
import subprocess
import sys

DATA = Path(__file__).resolve().parent
cutoff = datetime.datetime.fromisoformat('2026-10-31T11:05:00+00:00')
plan = json.loads((DATA / sys.argv[1]).read_text())
for name, args in plan:
    if datetime.datetime.now(datetime.UTC) >= cutoff:
        (DATA / (name + '-unrun.json')).write_text(json.dumps(dict(args=args, reason='Four-hour hold cutoff'), indent=2))
        continue
    rc = subprocess.call([sys.executable, str(DATA / 'run.py'), name, *args])
    print(name, rc, flush=True)
