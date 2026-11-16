from pathlib import Path
import json, os, shutil, subprocess
scratch=Path('/private/tmp/sf-hor-b1')
workspace=Path.cwd()
tracked=subprocess.check_output(['git','--git-dir',str(scratch/'git'),'ls-files','-z'],text=True).split('\0')
rows=[]
# Record each fixture-created folder outside scratch, including ones already removed.
for file in sorted(scratch.glob('outside-*.txt')):
    for text in file.read_text().splitlines():
        path=Path(text)
        assert 'sf-hor-b1' in str(path)
        assert path.is_relative_to(workspace)
        existed=path.exists()
        if existed:
            assert 'sf-hor-b1' in str(path)
            shutil.rmtree(path)
        rows.append({'path':str(path),'action':'guarded removal' if existed else 'fixture already removed with job guard'})
# These did not exist in the provided clean checkout; keep every tracked file.
candidates=[]
for dirname in ('.venv','.pytest_cache','build'):
    path=workspace/dirname
    if path.exists():
        candidates.append(path)
for root, directories, _ in os.walk(workspace):
    directories[:]=[name for name in directories if workspace/name not in candidates and name!='.git']
    if '__pycache__' in directories:
        candidates.append(Path(root)/'__pycache__')
        directories.remove('__pycache__')
cleanup=scratch/'cleanup'
cleanup.mkdir(exist_ok=True)
for number,path in enumerate(candidates):
    relative=str(path.relative_to(workspace))+'/'
    assert not any(name.startswith(relative) for name in tracked),path
    destination=cleanup/f'{number}-{path.name}'
    shutil.move(str(path),str(destination))
    assert 'sf-hor-b1' in str(destination)
    shutil.rmtree(destination)
    rows.append({'path':str(path),'action':'moved to '+str(destination)+'; guarded shutil.rmtree'})
# Remove completed test roots/caches, retaining private Git metadata and small evidence files.
for path in list(scratch.iterdir()):
    if path.is_dir() and path.name!='git':
        assert 'sf-hor-b1' in str(path)
        shutil.rmtree(path)
(scratch/'cleanup.json').write_text(json.dumps(rows,indent=2)+'\n')
print('Recorded and removed',len(rows),'outside-scratch folders; all tracked files preserved.')
print('Completed basetemps, dependency/Swift/Hypothesis caches and job fake roots removed.')
