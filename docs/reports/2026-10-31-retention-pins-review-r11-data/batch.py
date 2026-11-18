import json, os, signal, subprocess, sys
from pathlib import Path
D=Path(__file__).resolve().parent
plan=json.loads((D/sys.argv[1]).read_text())
for name,args in plan:
 p=subprocess.Popen([sys.executable,str(D/'run.py'),name,*args],env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
 try:
  rc=p.wait()
 except BaseException:
  if p.poll() is None: p.send_signal(signal.SIGINT)
  p.wait()
  raise
 print(name,rc,flush=True)
