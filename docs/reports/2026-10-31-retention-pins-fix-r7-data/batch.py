"""Sequential holds; this process remains owned by the calling exec session."""
import json
from pathlib import Path
import subprocess
import sys
base = Path(__file__).resolve().parent
plan = json.loads((base / sys.argv[1]).read_text())
for name, args in plan:
    rc = subprocess.call([sys.executable, str(base / 'run.py'), name, *args])
    print(name, rc, flush=True)
