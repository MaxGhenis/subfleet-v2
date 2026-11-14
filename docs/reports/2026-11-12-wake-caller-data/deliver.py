from pathlib import Path
import datetime
import json
import os
import shutil
import subprocess

workspace=Path.cwd()
scratch=Path('/private/tmp/sf-wc-b1')
test_root=Path('/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261111-122601-wc-build-r1s-a1-uwzfonyi/sf-wc-b1')
data=workspace/'docs/reports/2026-11-12-wake-caller-data'
metadata=scratch/'git-local'
base='b51f08f961e1b1dcfb93347eb080f9ddd9a9260d'
env=dict(os.environ,GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_NOSYSTEM='1')
git=['/usr/bin/git','--git-dir='+str(metadata),'--work-tree='+str(workspace)]
commands=[]
def run(args):
 result=subprocess.run(args,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
 commands.append({'command':args,'rc':result.returncode,'output':result.stdout})
 if result.returncode:raise RuntimeError(result.stdout)
 return result.stdout.strip()
head=run(git+['rev-parse','HEAD'])
run(git+['diff','--check'])
bundle=data/'delivery.gitbundle'
assert not bundle.exists()
run(git+['bundle','create',str(bundle),base+'..feat/wake-caller-217'])
run(git+['bundle','verify',str(bundle)])
heads=run(git+['bundle','list-heads',str(bundle)])
assert head in heads
status=run(git+['status','--porcelain'])
expected='?? '+str(bundle.relative_to(workspace))
assert status == expected, status
def writable_dirs(folder):
 os.chmod(folder,0o700)
 with os.scandir(folder) as entries:
  children=[Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)]
 for child in children:writable_dirs(child)
cleanup=[]
for owned in (test_root,scratch):
 assert owned.name=='sf-wc-b1' and 'sf-wc-b1' in str(owned) and not owned.is_symlink()
 assert (owned/'OWNER').read_text().strip()==str(workspace)
 writable_dirs(owned)
 shutil.rmtree(owned)
 cleanup.append({'path':str(owned),'guard':'sf-wc-b1 in path; basename; no symlink; matching OWNER','removed':not owned.exists()})
receipt={'head':head,'base':base,'branch':'feat/wake-caller-217','commands':commands,
 'bundle':str(bundle.relative_to(workspace)),'cleanup':cleanup,
 'completed_at':datetime.datetime.now(datetime.UTC).isoformat()}
(data/'delivery-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,indent=2))
