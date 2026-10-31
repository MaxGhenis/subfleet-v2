import json, subprocess, sys
from pathlib import Path
p=Path(__file__).resolve().parent
for name,args in json.loads((p/sys.argv[1]).read_text()):
 if (p/"pause-after-linked").exists() and not name.startswith("linked-"): break
 print("START",name,flush=True)
 rc=subprocess.call([sys.executable,str(p/"run.py"),name,*args])
 print("END",name,rc,flush=True)
