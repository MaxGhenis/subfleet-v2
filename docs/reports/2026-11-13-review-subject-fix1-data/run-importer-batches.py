import json, os, signal, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path('/private/tmp/sf-a2-fix1')
BASE=Path('/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261115-180648-rv-a2-fix-r1b-a1-3hqfxg5e/sf-a2-fix1')
BATCHES=json.loads((ROOT/'importer-batches.json').read_text())
DEADLINE=datetime(2026,11,16,2,25,tzinfo=timezone.utc).timestamp()
start=int(sys.argv[1])
for index in range(start,len(BATCHES)):
    if time.time() >= DEADLINE:
        print('BATCH DEADLINE',index,flush=True)
        raise SystemExit(82)
    if (ROOT/'stop-after-batch').exists():
        print('BATCH PAUSED',index,flush=True)
        raise SystemExit(83)
    paths=BATCHES[index]
    (ROOT/'active-selection.txt').write_text('\n'.join(paths)+'\n')
    args=['uv','run','--no-sync','--with','pytest-xdist>=3.6','python',str(ROOT/'run4.py'),'-q','-n','4','--dist','worksteal','--timeout=900','-m','not real_lsof and not real_cwd','tests/unit','tests/fake',f'--basetemp={BASE}/pytest-importer-batch-{index:02d}']
    print('BATCH START',index,len(paths),flush=True)
    began=time.monotonic()
    with (ROOT/f'importer-batch-{index:02d}.log').open('w') as log:
        child=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT)
        while child.poll() is None:
            try:
                child.wait(timeout=min(15,max(1,DEADLINE-time.time())))
            except subprocess.TimeoutExpired:
                if time.time() >= DEADLINE:
                    child.send_signal(signal.SIGINT)
                    try: child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        child.terminate()
                        try: child.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            child.kill(); child.wait()
    record={'batch':index,'files':paths,'returncode':child.returncode,'seconds':round(time.monotonic()-began,2)}
    with (ROOT/'batch-results.jsonl').open('a') as result:result.write(json.dumps(record)+'\n')
    print('BATCH DONE',index,child.returncode,record['seconds'],flush=True)
    if child.returncode:
        raise SystemExit(child.returncode)
