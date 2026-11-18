"""Review-only stat comparison for every canonical call in a round-file hold."""
import collections, json, os
from pathlib import Path
from subfleet import folders
COUNTS=collections.Counter(); CHANGES=[]; NODE=""
def outcome(path):
    try:
        s=os.stat(path); return [0,s.st_dev,s.st_ino]
    except (OSError,ValueError) as e: return [getattr(e,"errno",type(e).__name__)]
def pytest_runtest_setup(item):
    global NODE
    NODE=item.nodeid
def pytest_configure(config):
    original=folders.canonical
    def observed(path):
        before=outcome(path)
        value=original(path)
        after=outcome(value)
        COUNTS[(str(before[0]),str(after[0]))]+=1
        if before[0]==0 and after[0]!=0 or before[0]==0 and after[0]==0 and before!=after:
            CHANGES.append(dict(node=NODE,raw=os.fspath(path),spelled=value,before=before,after=after))
        return value
    config._r12_canonical=original
    folders.canonical=observed
def pytest_unconfigure(config):
    folders.canonical=config._r12_canonical
def pytest_sessionfinish(session,exitstatus):
    Path(__file__).with_name("canonical-"+os.environ["SF_R11_HOLD_NAME"]+".json").write_text(json.dumps(dict(counts={str(k):v for k,v in COUNTS.items()},changes=CHANGES),indent=2))
