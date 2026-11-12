from pathlib import Path
import os
import re
import subprocess
root=Path.cwd()
data=root/'docs/reports/2026-11-12-wake-caller-data'
env=dict(os.environ,GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_NOSYSTEM='1')
git=['/usr/bin/git','--git-dir=/private/tmp/sf-wc-b1/git-local','--work-tree='+str(root)]
base='b51f08f961e1b1dcfb93347eb080f9ddd9a9260d'
head=subprocess.check_output(git+['rev-parse','HEAD'],env=env,text=True).strip()
patch=subprocess.check_output(git+['diff','--binary',base,head],env=env,text=True)
(data/'implementation.patch').write_text(patch)
lines=['Implementation head: '+head, 'Base: '+base]
for line in patch.splitlines():
 if line.startswith('+++ b/'):lines.append('\n'+line[6:])
 elif line.startswith('@@'):lines.append(line)
(data/'change-lines.txt').write_text('\n'.join(lines)+'\n')
print(head)
