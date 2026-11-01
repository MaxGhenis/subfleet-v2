"""Generated-only reversion trials, one scratch copy and one pytest per hold."""
import ast
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

def call_waited(command, **kwargs):
    # subprocess.call kills its direct child on KeyboardInterrupt, before that
    # child's finally can release its separately grouped lock command.
    child = subprocess.Popen(command, **kwargs)
    try:
        return child.wait()
    except KeyboardInterrupt:
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
        child.wait()  # finish descendant cleanup before removing its source copy
        raise


DATA=Path(__file__).resolve().parent
ROOT=DATA.parents[2]
SCRATCH=Path('/private/tmp/sf-fix272r8')
source=(ROOT/'subfleet/dependencies.py').read_text()
a=source.index('    @staticmethod\n    def _target(path: str)')
b=source.index('    def resolve(',a)
old_target='''    @staticmethod
    def _target(path: str) -> tuple:
        resolved = os.path.realpath(path)
        try:
            info = os.stat(path)
            identity = (info.st_dev, info.st_ino)
        except (FileNotFoundError, NotADirectoryError):
            identity = None
        return resolved, identity, ()

'''
mutants=json.loads((DATA/'mutants.json').read_text())
trials={
 'decision1': [(source[a:b],old_target)],
 'decision2': [(edit['old'],edit['new']) for edit in mutants['r8-pointer-file-location-recording-dropped']['edits']],
 'decision3': [(edit['old'],edit['new']) for edit in mutants['r8-top-level-links-dropped']['edits']],
 'r7-f1': [(edit['old'],edit['new']) for edit in mutants['r7-unused-parent-pointer-probing-restored']['edits']],
}
manifest={}
for name,edits in trials.items():
    if datetime.datetime.now(datetime.UTC) >= datetime.datetime.fromisoformat('2026-10-31T11:05:00+00:00'):
        manifest[name]={'unrun':'Four-hour hold cutoff'}
        continue
    copy=SCRATCH/('reversion-'+name)
    copy.mkdir(exist_ok=True)
    try:
        for folder in ('subfleet','tests'):
            shutil.copytree(ROOT/folder,copy/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        shutil.copy2(ROOT/'pyproject.toml',copy/'pyproject.toml')
        changed=source
        for before,after in edits:
            assert changed.count(before)==1,(name,before)
            changed=changed.replace(before,after)
        (copy/'subfleet/dependencies.py').write_text(changed)
        removed=[]
        pointer=copy/'tests/unit/test_retention_pointer_inputs.py'
        text=pointer.read_text()
        lines=text.splitlines(keepends=True)
        for node in ast.walk(ast.parse(text)):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if isinstance(decorator,ast.Call) and isinstance(decorator.func,ast.Name) and decorator.func.id=='example':
                        removed.append((decorator.lineno,decorator.end_lineno))
        for first,last in reversed(sorted(removed)):
            del lines[first-1:last]
        pointer.write_text(''.join(lines))
        manifest[name]=dict(edits=edits, source_hash=hashlib.sha256(changed.encode()).hexdigest(),
                            removed_explicit_examples=len(removed), properties={})
        for relative,func in [('tests/unit/test_retention_location_inputs.py','test_location_inputs_keep_the_places_real_git_traverses'),
                              ('tests/unit/test_retention_pointer_inputs.py','test_named_own_inputs_keep_every_store_real_git_reads')]:
            original=ast.parse((ROOT/relative).read_text())
            trial=ast.parse((copy/relative).read_text())
            def digest(tree):
                node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==func)
                return hashlib.sha256(ast.dump(ast.Module(body=node.body,type_ignores=[])).encode()).hexdigest()
            assert digest(original)==digest(trial)
            manifest[name]['properties'][func]=dict(body_sha256=digest(original), unchanged=True)
        (DATA/'reversions-manifest.json').write_text(json.dumps(manifest,indent=2))
        for prop,node in [('location','tests/unit/test_retention_location_inputs.py::test_location_inputs_keep_the_places_real_git_traverses'),
                          ('focused','tests/unit/test_retention_pointer_inputs.py::test_named_own_inputs_keep_every_store_real_git_reads')]:
            env=dict(os.environ,SF_R8_SOURCE=str(copy))
            rc=call_waited([sys.executable,str(DATA/'run.py'),'revert-'+name+'-'+prop,'--linked',node],env=env)
            print(name,prop,rc,flush=True)
    finally:
        assert '/sf-' in str(copy)
        shutil.rmtree(copy)
(DATA/'reversions-manifest.json').write_text(json.dumps(manifest,indent=2))
