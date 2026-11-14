"""Run only on root's GO; each mutation is restored from pristine bytes."""
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import sys

WORK = Path('/Users/maxghenis/.subfleet/worktrees/20261111-122601-wc-build-r1s')
ARTIFACTS = Path('/private/tmp/sf-wc-b1/mutants')
ENGINE = WORK / 'subfleet/caller_wakes.py'
NUDGE = WORK / 'subfleet/sessions/nudge.py'
TEST = 'tests/fake/test_caller_notice_wakes.py::'
ARTIFACTS.mkdir(exist_ok=True)
pristine = {path: path.read_bytes() for path in (ENGINE, NUDGE)}
for path, value in pristine.items():
    (ARTIFACTS / (path.name + '.pristine')).write_bytes(value)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def clear_module_caches():
    root = Path('/private/tmp/sf-wc-b1/pycache')
    for pattern in ('caller_wakes.*.pyc', 'nudge.*.pyc'):
        for cache in root.rglob(pattern):
            assert 'sf-wc-b1' in str(cache)
            cache.unlink()


def no_state_guards(source):
    return source.replace("n.state IN ('pending','offered') AND ", '').replace(
        "state IN ('pending','offered') AND ", '')


def wake_twice(source):
    return no_state_guards(source).replace('n.wake_state IS NULL AND ', '').replace(
        ' AND wake_state IS NULL', '').replace('"AND wake_state IS NULL"', '""')


def lose_record(source):
    target = '        with self.store.transaction("notice.wake_recovered") as tx:\n'
    assert source.count(target) == 1
    return source.replace(target, target +
        '            tx.execute("UPDATE notices SET state=\'pending\',wake_state=NULL,wake_at=NULL "\n'
        '                       "WHERE wake_state=\'delivered\'")\n')


def output_text(source):
    target = '                result["sessions"] += 1\n'
    assert source.count(target) == 1
    return source.replace(target, '                headers += [row["text"] for row in claimed]\n' + target)


def conversation(source):
    old1 = '            if self.daemon._conversation_binding(session):\n                continue  # C-24.10 is the only wake path for this session.\n'
    old2 = '                    if self.daemon._conversation_binding(session) or not self._eligibility(\n'
    assert source.count(old1) == source.count(old2) == 1
    return source.replace(old1, '').replace(old2, '                    if not self._eligibility(\n')


mutants = [
    ('wake_twice', ENGINE, wake_twice,
     'test_c15_9_delivered_notice_survives_restart_without_replay'),
    ('hook_shown', ENGINE, no_state_guards,
     'test_c15_9_hook_shown_notice_never_wakes'),
    ('drop_quiet_gate', NUDGE,
     lambda src: src.replace('    if age_s < limits["notice_quiet_s"]:\n', '    if False:\n'),
     'test_c15_9_quiet_only_never_wakes_a_recently_active_session'),
    ('lose_record_restart', ENGINE, lose_record,
     'test_c15_9_delivered_notice_survives_restart_without_replay'),
    ('output_text', ENGINE, output_text,
     'test_c15_9_scrubbed_wake_uses_header_fields_never_notice_or_output_text'),
    ('conversation', ENGINE, conversation,
     'test_c15_9_one_path_conversation_bound_session_is_left_to_wake_engine'),
]
results = []
for name, path, mutation, killer in mutants:
    assert all(p.read_bytes() == data for p, data in pristine.items())
    before = pristine[path]
    after = mutation(before.decode()).encode()
    assert before != after, name
    patch = ''.join(difflib.unified_diff(before.decode().splitlines(keepends=True),
                                       after.decode().splitlines(keepends=True),
                                       fromfile=str(path.relative_to(WORK)),
                                       tofile=str(path.relative_to(WORK))))
    (ARTIFACTS / (name + '.patch')).write_text(patch)
    item = {'name': name, 'path': str(path.relative_to(WORK)), 'killer': killer,
            'pristine_sha256': sha(before), 'mutant_sha256': sha(after),
            'patch': str(ARTIFACTS / (name + '.patch'))}
    try:
        path.write_bytes(after)
        clear_module_caches()
        cmd = ['python3', '/private/tmp/sf-wc-b1/run.py', 'mutant-' + name,
               'pytest', '-q', TEST + killer]
        result = subprocess.run(cmd, cwd=WORK, text=True, capture_output=True)
        log = Path('/private/tmp/sf-wc-b1/mutant-' + name + '.log')
        value = log.read_text(errors='replace')
        item.update(command=cmd, rc=result.returncode, log=str(log),
                    killed=result.returncode == 1 and 'AssertionError' in value and
                           'FAILED ' + TEST + killer in value)
        print(json.dumps(item), flush=True)
    finally:
        for p, data in pristine.items():
            p.write_bytes(data)
        clear_module_caches()
        item['restored_sha256'] = sha(path.read_bytes())
        item['all_pristine_restored'] = all(p.read_bytes() == data for p, data in pristine.items())
        assert item['all_pristine_restored']
        results.append(item)
        (ARTIFACTS / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
assert len(results) == 6 and all(item['killed'] for item in results)
