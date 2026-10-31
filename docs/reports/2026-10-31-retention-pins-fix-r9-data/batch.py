"""Foreground sequential holds only; every subprocess is waited to completion."""
import datetime
import json
from pathlib import Path
import signal
import subprocess
import sys

def call_waited(command, **kwargs):
    # subprocess.call kills its direct child on KeyboardInterrupt, before that
    # child's finally can release its separately grouped lock command.
    child = subprocess.Popen(command, **kwargs)
    try:
        return child.wait()
    except KeyboardInterrupt:
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
        child.wait()  # finish descendant cleanup before removing its source copy
        raise


DATA = Path(__file__).resolve().parent
cutoff = datetime.datetime.fromisoformat('2026-10-31T13:56:57+00:00')
plan = json.loads((DATA / sys.argv[1]).read_text())
for name, args in plan:
    if datetime.datetime.now(datetime.UTC) >= cutoff:
        (DATA / (name + '-unrun.json')).write_text(json.dumps(dict(args=args, reason='Four-hour hold cutoff'), indent=2))
        continue
    rc = call_waited([sys.executable, str(DATA / 'run.py'), name, *args])
    print(name, rc, flush=True)
