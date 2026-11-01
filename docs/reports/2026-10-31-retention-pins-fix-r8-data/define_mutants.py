"""The twenty requested r7 mutants and nine named r8 mutations only."""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]
source = (ROOT / 'subfleet/dependencies.py').read_text()
old = json.loads((ROOT / 'docs/reports/2026-10-31-retention-pins-fix-r7-data/mutants.json').read_text())
old['r3-post-traversal-alias-check-dropped']['edits'][0].update(
    old='    aliases.check(incomplete)\n    recorded = list(dict.fromkeys(folders.canonical(each) for each in found))',
    new='    recorded = list(dict.fromkeys(folders.canonical(each) for each in found))')
a=source.index('        inferred = os.path.dirname(os.path.dirname(gitdir))')
b=source.index('    aliases.scan(common, incomplete)', a)
old['r6-parent-common-fallback-restored']['edits'][0].update(old=source[a:b],new='        common = os.path.dirname(os.path.dirname(gitdir))\n')
a=source.index('        parent_objects = os.path.join(inferred, "objects")')
b=source.index('        alternates = _alternates(own_objects', a)
old['r7-unused-parent-pointer-probing-restored']['edits'][0].update(old=source[a:b])
for name, mutation in old.items():
    mutation['previous_result'] = 'docs/reports/2026-10-31-retention-pins-fix-r7-data/mutant-' + name + '.json'
    mutation['tests'] = [item.replace('--confcutdir=/Users/maxghenis/.subfleet/worktrees/20261030-223353-pr272-fix-r7/', '--confcutdir=') for item in mutation['tests']]


def add(name, edits, test):
    old[name] = dict(edits=[dict(module='subfleet.dependencies', old=a, new=b) for a,b in edits], tests=[test])


UNIT='tests/unit/test_retention_locations.py::'
REVIEW='tests/fake/test_retention_dependency_review_r8.py::test_running_job_keeps_tree_containing_required_git_pointer'
add('r8-alias-location-recording-dropped', [
    ('        self.links.update(link[0] for link in target[2])\n','')], REVIEW+'[admin-path]')
add('r8-pointer-file-location-recording-dropped', [
    ('            self.files.add(resolved)\n',''),
    ('                    aliases.files.add(resolved)\n','')], REVIEW+'[gitfile]')
add('r8-intermediate-link-walk-dropped', [
    ('                    links.append((location, target, (info.st_dev, info.st_ino)))',
     '                    if not pending:\n                        links.append((location, target, (info.st_dev, info.st_ino)))')],
    UNIT+'test_required_locations_survive_two_relative_links[intermediate-HEAD]')
add('r8-lexical-dotdot', [
    ('        pending = deque((path if os.path.isabs(path)',
     '        path = os.path.abspath(path)\n        pending = deque((path if os.path.isabs(path)')],
    UNIT+'test_dotdot_applies_after_the_link_before_it')
add('r8-link-location-followed', [
    ('                    location = os.path.join(folders.canonical(parent), part)',
     '                    location = folders.canonical(entry)')],
    UNIT+'test_required_locations_survive_two_relative_links[gitfile-target-HEAD]')
add('r8-top-level-links-dropped', [
    ('        if directory in self.scanned:\n', '        return\n        if directory in self.scanned:\n')],
    UNIT+'test_required_locations_survive_two_relative_links[git-entry-HEAD]')
add('r8-parent-hint-scanned', [
    ('        inferred = os.path.dirname(os.path.dirname(gitdir))\n',
     '        inferred = os.path.dirname(os.path.dirname(gitdir))\n        aliases.scan(inferred, incomplete)\n')],
    UNIT+'test_inferred_parent_is_not_scanned')
add('r8-walk-revalidation-dropped', [
    ('                if self._target(path) == before:',
     '                if self._target(path)[:2] == before[:2]:')],
    UNIT+'test_changed_link_chain_with_the_same_final_target_is_incomplete')
a=source.index('        locations = {place for place in self.links | self.files')
b=source.index('\n\n    def check',a)
add('r8-routine-skip-dropped', [(source[a:b], '        return sorted(self.links | self.files | self.entries)')],
    UNIT+'test_routine_records_are_exactly_unchanged[worktree]')
for name, mutation in old.items():
    sources={}
    for edit in mutation['edits']:
        modulepath=ROOT / (edit['module'].replace('.','/')+'.py')
        text=sources.get(edit['module'],modulepath.read_text())
        assert text.count(edit['old'])==1,(name,edit['old'],text.count(edit['old']))
        sources[edit['module']]=text.replace(edit['old'],edit['new'])
    for mod, text in sources.items(): compile(text, mod, 'exec')
(DATA/'mutants.json').write_text(json.dumps(old,indent=2)+'\n')
(DATA/'mutants-plan.json').write_text(json.dumps([['mutant-'+name,['--mutant',name,*mutation['tests']]] for name,mutation in old.items()],indent=2)+'\n')
print(len(old), 'validated definitions')
