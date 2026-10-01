import json
import subprocess
import sys
from pathlib import Path
here=Path("docs/reports/2026-10-01-retention-81-notes")
slices=json.loads((here/"notes-remaining.json").read_text())
results=[]
for i,nodes in enumerate(slices,1):
    print(f"Remaining notes foreground slice {i}/{len(slices)}",flush=True)
    cmd=[sys.executable,str(here/"run_check.py"),str(here/f"notes-final-slice-{i:02d}.txt"),"550","uv","run","pytest","-q",*nodes]
    p=subprocess.run(cmd)
    results.append({"slice":i,"exit":p.returncode,"tests":nodes})
    (here/"notes-remaining-results.json").write_text(json.dumps(results,indent=2)+"\n")
    if p.returncode:raise SystemExit(p.returncode)
print(f"All {sum(len(s) for s in slices)} remaining note cases passed.",flush=True)
