"""Sequential, waited verification holds; no detached processes."""
from pathlib import Path
import sys
from batch import call_waited
D=Path(__file__).resolve().parent
for command in [
    [sys.executable,str(D/'run.py'),'edges-final','review-r10-data/edge_probes.py'],
    [sys.executable,str(D/'batch.py'),'mutants-plan.json'],
    [sys.executable,str(D/'reversions.py')],
]:
    rc=call_waited(command)
    print('verification step',Path(command[1]).name,'exit',rc,flush=True)
