"""Run only the thirteen named mutants, then four single-site reversions."""
from pathlib import Path
import sys
from batch import call_waited
p = Path(__file__).resolve().parent
call_waited([sys.executable, str(p / "batch.py"), "mutants-plan.json"])
call_waited([sys.executable, str(p / "reversions.py")])
