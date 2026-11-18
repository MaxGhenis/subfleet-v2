import json
from pathlib import Path
import pytest
from subfleet import retention_archive as rarch
from tests.fake.test_retention_dependency_review_r2 import world, maintain

@pytest.mark.parametrize('phase', ['quarantine', 'archive', 'final_check'])
def test_old_journal_paths(world, monkeypatch, phase):
    tree = world.job('owner')
    directory = world.root / 'jobs' / 'owner'
    class Crash(BaseException): pass
    def crash(self, *args, **kwargs): raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(rarch.Retirement, phase, crash)
        with pytest.raises(Crash): maintain(world)
    path = rarch.journal_path(world.root, 'owner')
    journal = json.loads(path.read_text())
    assert 'job_folder' in journal
    journal.pop('job_folder')
    path.write_text(json.dumps(journal))
    final = rarch.Retirement.final_check
    rename = rarch.os.rename
    checks, moves = [], []
    def observing(self):
        checks.append(('job_folder' in self.journal, 'job_folder' in json.loads(path.read_text())))
        return final(self)
    def moving(source, target, *args, **kwargs):
        if str(source) in (str(tree), str(directory), str(world.root / 'retention' / 'owner' / 'archive')):
            moves.append((str(source), 'job_folder' in json.loads(path.read_text())))
        return rename(source, target, *args, **kwargs)
    monkeypatch.setattr(rarch.Retirement, 'final_check', observing)
    monkeypatch.setattr(rarch.os, 'rename', moving)
    result = maintain(world)
    assert result['pruned'] == ['owner'], result
    assert checks == [(phase == 'quarantine', phase == 'quarantine')], (phase, checks, moves)
    assert moves and all(saved == (phase == 'quarantine') for _,saved in moves), moves
    print('OLD-JOURNAL', phase, 'starting-state',journal['state'],'final',checks,'renames',moves)
