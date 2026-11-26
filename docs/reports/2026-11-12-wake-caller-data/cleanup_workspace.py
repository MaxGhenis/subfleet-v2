from pathlib import Path
import json
import os
import shutil

workspace=Path.cwd()
scratch=Path('/private/tmp/sf-wc-b1')
tracked=set((scratch/'committed-files.txt').read_text().splitlines())
owned_names={'.pytest_cache','.hypothesis','__pycache__','build','subfleet.egg-info','.uv-cache','.venv'}
candidates=[]
for here,dirs,files in os.walk(workspace):
    dirs[:]=[name for name in dirs if name not in {'.git','.git-local','node_modules'}]
    for name in list(dirs):
        if name in owned_names:
            path=Path(here)/name
            relative=path.relative_to(workspace).as_posix()
            if any(f==relative or f.startswith(relative+'/') for f in tracked):
                continue
            candidates.append(path)
            dirs.remove(name)
results=[]
for index,path in enumerate(candidates):
    target=scratch/'workspace-caches'/str(index)
    target.parent.mkdir(parents=True,exist_ok=True)
    assert 'sf-wc-b1' in str(target) and not target.exists()
    if path.is_symlink():
        path.unlink()
        results.append({'path':str(path),'operation':'unlink job cache symlink'})
    else:
        shutil.move(str(path),str(target))
        assert 'sf-wc-b1' in str(target)
        shutil.rmtree(target)
        results.append({'path':str(path),'operation':'move into scratch then guarded shutil.rmtree'})
out=workspace/'docs/reports/2026-11-12-wake-caller-data/workspace-cleanup.json'
out.write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps(results,indent=2))
