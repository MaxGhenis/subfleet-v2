"""Guarded removal of this job's own fixture roots after every hold ends."""
import datetime,json,os,stat,subprocess,sys
from pathlib import Path
D=Path(__file__).resolve().parent;ROOT=D.parent
completion=json.loads((D/'completion.json').read_text());assert completion['final'],completion
roots=[Path('/private/tmp/sf-r272r11'),ROOT/'sf-r272r11-fixtures',ROOT/'sf-r272r10-fixtures']
record={'start':datetime.datetime.now(datetime.UTC).isoformat(),'roots':[]}
for root in roots:
 assert '/sf-' in str(root) and not root.is_symlink(),root
 if not root.exists():continue
 entry={'path':str(root),'allocated_bytes':0,'logical_bytes':0,'entries':0,'permissions_restored':0}
 root.chmod(stat.S_IMODE(root.stat().st_mode)|0o700)
 seen=set()
 for directory,names,files in os.walk(root,followlinks=False):
  for name in names+files:
   p=Path(directory)/name
   try:s=p.lstat()
   except FileNotFoundError:continue
   entry['entries']+=1
   identity=(s.st_dev,s.st_ino)
   if identity not in seen:
    seen.add(identity);entry['allocated_bytes']+=s.st_blocks*512;entry['logical_bytes']+=s.st_size
   if stat.S_ISDIR(s.st_mode) and stat.S_IMODE(s.st_mode)&0o700!=0o700:
    p.chmod(stat.S_IMODE(s.st_mode)|0o700);entry['permissions_restored']+=1
 entry['measured_at']=datetime.datetime.now(datetime.UTC).isoformat()
 assert entry['allocated_bytes']<1.5*1024**3,entry
 record['roots'].append(entry)
 (D/'cleanup-progress.json').write_text(json.dumps(record,indent=2))
 command=[sys.executable,'-c','import shutil,sys; p=sys.argv[1]; assert "/sf-" in p, p; shutil.rmtree(p, ignore_errors=True)',str(root)]
 subprocess.run(command,check=True)
 assert not root.exists(),root
 entry['removed']=True
 record['end']=datetime.datetime.now(datetime.UTC).isoformat()
 (D/'cleanup.json').write_text(json.dumps(record,indent=2))
record['all_removed']=all(not p.exists() for p in roots)
(D/'cleanup.json').write_text(json.dumps(record,indent=2));print(json.dumps(record,indent=2))
