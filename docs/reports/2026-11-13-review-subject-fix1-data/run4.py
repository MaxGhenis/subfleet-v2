import json, os, sys, time
from pathlib import Path
ROOT=Path('/private/tmp/sf-a2-fix1')
sys.path[:0]=[str(ROOT),str(Path.cwd())]
os.environ['PYTHONPATH']=str(ROOT)+os.pathsep+str(Path.cwd())
from install_guard import install
install()
sys.stdout.reconfigure(line_buffering=True, write_through=True)
sys.stderr.reconfigure(line_buffering=True, write_through=True)
import pytest
class Counts:
    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        reporter=session.config.pluginmanager.get_plugin('terminalreporter')
        self.counts={key:len(values) for key,values in reporter.stats.items()}
plugin=Counts()
started=time.monotonic()
args=['-p','sf_fix1_safety',*sys.argv[1:]]
code=pytest.main(args, plugins=[plugin])
record={'pid':os.getpid(),'selected_files':(ROOT/'active-selection.txt').read_text().splitlines() if (ROOT/'active-selection.txt').exists() else None,'args':args,'exit':int(code),'seconds':round(time.monotonic()-started,2),'counts':getattr(plugin,'counts',{})}
with (ROOT/'ledger.jsonl').open('a') as f:
    f.write(json.dumps(record)+'\n')
print('LEDGER',json.dumps(record),flush=True)
raise SystemExit(code)
